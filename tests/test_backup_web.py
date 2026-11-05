import io
import re
import tarfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from laminator import backup
from laminator.web import app as webapp
from laminator.web.app import create_app
from test_backup import make_root, names, rewrite
from test_web import FakeMdb, FakeServer, finished, login

CFG = {"servers": {"emby": {"url": "http://x:8096"}}}
PASS = "a long enough passphrase"


@pytest.fixture
def ui(tmp_path, monkeypatch):
    for n in ("API_EMBY", "API_MDBLIST"):
        monkeypatch.delenv(n, raising=False)
    root = make_root(tmp_path / "data")
    (root / "admin.hash").unlink()   # a saved password would take precedence over the "pw" the tests log in with
    app = create_app(dict(CFG), {"emby": FakeServer()}, root, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, root


def make(c, tok, **kw):
    r = c.post("/settings/backup/create", data={"token": tok(), **kw})
    assert r.status_code == 303
    return r


def test_creating_a_backup_runs_as_a_job_and_lists_it(ui):
    app, c, tok, root = ui
    r = make(c, tok)
    log = finished(app, c, r.headers["location"])
    assert "laminator-backup-" in log and "done" in log and "not included" in log
    files = list((root / "backups").glob("*.tar.gz"))
    assert len(files) == 1 and "servers/emby/state.db" in names(files[0])
    page = c.get("/settings").text
    assert files[0].name in page and "Emby: 2 posters, 1 metadata" in page and "Download" in page and "Restore" in page
    assert "EMBYKEY-0123456789abcdef" not in page and "EMBYKEY" not in files[0].read_bytes().decode("latin-1")


def test_passphrase_rules_are_checked_before_anything_is_written(ui):
    app, c, tok, root = ui
    for kw in ({"include_secrets": "1", "passphrase": "short", "passphrase2": "short"},
               {"include_secrets": "1", "passphrase": PASS, "passphrase2": PASS + "x"}):
        r = c.post("/settings/backup/create", data={"token": tok(), **kw})
        assert "note=bkbad" in r.headers["location"]
    assert not list((root / "backups").glob("*.tar.gz")) if (root / "backups").exists() else True
    r = make(c, tok, include_secrets="1", passphrase=PASS, passphrase2=PASS)
    finished(app, c, r.headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    assert "secrets.enc" in names(f) and b"EMBYKEY" not in f.read_bytes() and PASS not in c.get("/jobs").text
    assert "encrypted keys" in c.get("/settings").text


def test_download_serves_only_real_backups_and_needs_a_login(ui):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    r = c.get(f"/backups/download/{f.name}")
    assert r.status_code == 200 and r.content == f.read_bytes() and "attachment" in r.headers["content-disposition"]
    for bad in ("..%2F..%2Fsettings.yaml", "settings.yaml", "laminator-backup-x.tar.gz", "%2Fetc%2Fpasswd", "laminator-backup-..%2Fx.tar.gz"):
        assert c.get(f"/backups/download/{bad}").status_code in (404, 307, 308), bad
    anon = TestClient(app, follow_redirects=False)
    assert anon.get(f"/backups/download/{f.name}").status_code == 303                       # not without logging in


def test_verify_job_reports_ok_and_catches_damage(ui):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    r = c.post("/backups/verify", data={"name": f.name, "token": tok()})
    assert "OK:" in finished(app, c, r.headers["location"])
    bad = rewrite(f, lambda n, d: (n, d + b"X") if n.endswith("8.img") else (n, d))
    bad.replace(root / "backups" / "laminator-backup-damaged.tar.gz")
    r = c.post("/backups/verify", data={"name": "laminator-backup-damaged.tar.gz", "token": tok()})
    log = finished(app, c, r.headers["location"])
    assert "checksum mismatch" in log and "with problems" in log


def test_upload_accepts_a_valid_backup_and_refuses_junk_and_oversize(ui, monkeypatch):
    app, c, tok, root = ui
    good = backup.create(root, root.parent / "elsewhere.tar.gz").path
    r = c.post("/backups/upload", data={"token": tok()}, files={"file": ("mine.tar.gz", good.read_bytes(), "application/gzip")})
    assert "note=bkup" in r.headers["location"]
    up = list((root / "backups").glob("laminator-backup-uploaded-*.tar.gz"))
    assert len(up) == 1 and backup.verify(up[0]).files > 0
    r = c.post("/backups/upload", data={"token": tok()}, files={"file": ("x.tar.gz", b"this is not a backup", "application/gzip")})
    assert "note=bkbad" in r.headers["location"] and "refused" in r.headers["location"].replace("+", " ")
    hostile = rewrite(good, lambda n, d: (n, d), extra=[("../../evil", b"x")])
    r = c.post("/backups/upload", data={"token": tok()}, files={"file": ("h.tar.gz", hostile.read_bytes(), "application/gzip")})
    assert "note=bkbad" in r.headers["location"]
    monkeypatch.setattr(webapp, "MAX_UPLOAD", 1000)
    r = c.post("/backups/upload", data={"token": tok()}, files={"file": ("big.tar.gz", good.read_bytes(), "application/gzip")})
    assert "note=bkbad" in r.headers["location"] and "limit" in r.headers["location"]
    assert not list((root / "backups").glob("*.part")) and len(list((root / "backups").glob("laminator-backup-uploaded-*.tar.gz"))) == 1
    assert c.post("/backups/upload", data={"token": "x"}, files={"file": ("a", b"b")}).status_code == 403


def test_restore_needs_the_typed_phrase_moves_old_files_aside_and_reloads(ui):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    before = (root / "settings.yaml").read_text()
    (root / "settings.yaml").write_text("changed: after the backup\n")
    page = c.get(f"/backups/restore?name={f.name}").text
    assert "settings.yaml" in page and "servers" in page and "moved aside" in page and "RESTORE" in page
    assert c.post("/backups/restore", data={"name": f.name, "phrase": "yes", "token": tok()}).status_code == 400
    assert (root / "settings.yaml").read_text() == "changed: after the backup\n"                 # nothing happened
    r = c.post("/backups/restore", data={"name": f.name, "phrase": "RESTORE", "token": tok()})
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "restored" in log and "settings and connections reloaded" in log and "done" in log
    assert (root / "settings.yaml").read_text() == before
    aside = next(root.glob("pre-restore-*"))
    assert (aside / "settings.yaml").read_text() == "changed: after the backup\n"                # the replaced file is kept
    assert c.get("/settings").status_code == 200 and c.get("/library").status_code == 200       # the UI is fine afterwards
    assert (root / "backups" / f.name).exists()                                                    # the backups folder was not touched


def test_a_wrong_passphrase_fails_the_restore_without_changing_anything(ui):
    app, c, tok, root = ui
    finished(app, c, make(c, tok, include_secrets="1", passphrase=PASS, passphrase2=PASS).headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    (root / "settings.yaml").write_text("changed: after the backup\n")
    assert "Passphrase" in c.get(f"/backups/restore?name={f.name}").text
    r = c.post("/backups/restore", data={"name": f.name, "phrase": "RESTORE", "passphrase": "the wrong passphrase", "token": tok()})
    log = finished(app, c, r.headers["location"])
    assert "wrong passphrase" in log and "with problems" in log
    assert (root / "settings.yaml").read_text() == "changed: after the backup\n" and not list(root.glob("pre-restore-*"))
    r = c.post("/backups/restore", data={"name": f.name, "phrase": "RESTORE", "passphrase": PASS, "token": tok()})
    assert "restored" in finished(app, c, r.headers["location"]) and (root / "secrets.env").read_text().startswith("API_EMBY=")


def test_backup_writes_need_csrf_origin_and_a_password(ui, tmp_path):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    forms = [("/settings/backup/create", {}), ("/backups/verify", {"name": f.name}), ("/backups/restore", {"name": f.name, "phrase": "RESTORE"})]
    for path, body in forms:
        assert c.post(path, data={**body, "token": ""}).status_code == 403, path
        assert c.post(path, data={**body, "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403, path
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app({}, {}, tmp_path / "ro", None), follow_redirects=False)
    for path, body in forms:
        assert ro.post(path, data={**body, "token": "x"}).status_code == 403
    assert ro.get(f"/backups/restore?name={f.name}").status_code == 403
    assert not list(root.glob("pre-restore-*"))
