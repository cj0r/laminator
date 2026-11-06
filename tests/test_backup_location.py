"""Choosing where the primary backups are kept (a folder saved in Settings, or LAMINATOR_BACKUP_DIR), with the default unchanged."""
import os
import time
from pathlib import Path

import pytest

from laminator import backup, config
from laminator.backup import BackupError
from test_backup import make_root, names, root  # noqa: F401
from test_backup_web import ui, make, finished  # noqa: F401


def point(root, folder):
    s = config.load_settings(root)
    s["backup_dir"] = str(folder)
    config.save_settings(root, s)


# ---- the setting ------------------------------------------------------------------------------------------------
def test_default_environment_and_settings_decide_the_folder_in_that_order(root, tmp_path, monkeypatch):
    monkeypatch.delenv(backup.ENV_DIR, raising=False)
    assert backup.configured_dir(root) == (root / "backups", "default")
    monkeypatch.setenv(backup.ENV_DIR, str(tmp_path / "from-env"))
    assert backup.configured_dir(root) == (tmp_path / "from-env", "environment")
    point(root, tmp_path / "from-settings")
    assert backup.configured_dir(root) == (tmp_path / "from-settings", "settings")      # what is saved in the UI wins
    monkeypatch.setenv(backup.ENV_DIR, "relative/path")
    s = config.load_settings(root); s.pop("backup_dir"); config.save_settings(root, s)
    assert backup.configured_dir(root)[1] == "default"                                   # a relative value is ignored, not guessed at


def test_the_value_is_validated(root, tmp_path):
    ok = backup.validate_location(str(tmp_path / "nas" / "laminator") + "/", root)
    assert ok == str(tmp_path / "nas" / "laminator")
    for bad in ("relative", "", "/a/../b", "/x\x00y", "/" + "a" * 600, str(root), str(root / "servers"), str(root / "servers" / "emby" / "b"),
                str(root / "templates"), str(root / "fonts" / "x"), str(root / "cache")):
        with pytest.raises(ValueError):
            backup.validate_location(bad, root)
    assert backup.validate_location(str(root / "backups"), root)                          # the default folder itself is fine
    assert backup.validate_location(str(root / "my-backups"), root)                       # so is another folder inside the data folder


def test_writability_is_proved_with_a_plain_message(tmp_path):
    backup.check_writable(tmp_path / "new" / "deep")
    assert (tmp_path / "new" / "deep").is_dir() and not list((tmp_path / "new" / "deep").iterdir())    # the probe file is gone
    f = tmp_path / "afile"; f.write_text("x")
    with pytest.raises(BackupError, match="cannot be used"):
        backup.check_writable(f / "sub")
    if os.geteuid() != 0:
        ro = tmp_path / "ro"; ro.mkdir(); ro.chmod(0o500)
        with pytest.raises(BackupError, match="not writable"):
            backup.check_writable(ro)
        ro.chmod(0o700)


# ---- every backup function follows it -----------------------------------------------------------------------------
def test_create_list_and_scheduled_pruning_use_the_chosen_folder(root, tmp_path):
    dest = tmp_path / "other-disk"
    point(root, dest)
    rep = backup.create(root)
    assert rep.path.parent == dest and not (root / "backups").exists()
    assert [r.path for r in backup.list_backups(root)] == [rep.path]
    for i in range(3):
        backup.create_auto(root, keep=2)
        time.sleep(1.05)                                                                  # the file name carries the second
    autos = sorted(dest.glob(backup.AUTO_PREFIX + "*.tar.gz"))
    assert len(autos) == 2 and rep.path.exists()                                          # only scheduled ones were pruned, in that folder
    assert backup.verify(rep.path).files == rep.files
    assert "servers/emby/state.db" in names(rep.path)


def test_the_listing_does_not_show_or_remember_backups_of_the_old_folder(root, tmp_path):
    old = backup.create(root)
    assert [r.path.name for r in backup.list_backups(root)] == [old.path.name]
    point(root, tmp_path / "elsewhere")
    assert backup.list_backups(root) == []
    new = backup.create(root)
    assert [r.path for r in backup.list_backups(root)] == [new.path]
    loc = backup.location_info(root)
    assert loc["path"] == str(tmp_path / "elsewhere") and loc["count"] == 1 and loc["stranded"] == 1 and loc["ok"]


def test_an_unusable_folder_is_reported_not_raised_when_listing(root, tmp_path):
    f = tmp_path / "afile"; f.write_text("x")
    point(root, f / "backups")
    assert backup.list_backups(root) == []
    loc = backup.location_info(root)
    assert not loc["ok"] and "cannot be used" in loc["error"]
    with pytest.raises(BackupError):
        backup.create(root)


# ---- moving ---------------------------------------------------------------------------------------------------------
def three(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for n in ("20261101-010101", "20261102-010101", "auto-20261103-010101"):
        f = folder / f"laminator-backup-{n}.tar.gz"
        f.write_bytes(os.urandom(5000))
        out.append(f)
    (folder / "laminator-backup-x.tar.gz.part").write_bytes(b"half")
    (folder / "notes.txt").write_text("not a backup")
    return out


def test_moving_copies_checks_and_only_then_removes(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    files = three(src)
    data = {f.name: f.read_bytes() for f in files}
    assert sorted(backup.move_backups(src, dst)) == sorted(data)
    assert all((dst / n).read_bytes() == b for n, b in data.items()) and not list(src.glob("laminator-backup-*.tar.gz"))
    assert (src / "laminator-backup-x.tar.gz.part").exists() and (src / "notes.txt").exists()   # anything else is left alone
    assert not list(dst.glob("*.part")) and all(oct((dst / n).stat().st_mode & 0o777) == "0o600" for n in data)
    assert backup.move_backups(src, src) == [] and backup.move_backups(dst, dst) == []


def test_a_copy_that_does_not_verify_leaves_the_original_and_no_part_file(tmp_path, monkeypatch):
    src, dst = tmp_path / "src", tmp_path / "dst"
    files = three(src)
    real = backup._sha
    monkeypatch.setattr(backup, "_sha", lambda p: "bad" if p.suffix == ".part" else real(p))
    with pytest.raises(BackupError, match="did not copy cleanly"):
        backup.move_backups(src, dst)
    assert all(f.exists() for f in files) and not list(dst.glob("*"))


def test_an_existing_identical_file_is_not_duplicated_and_a_different_one_stops_the_move(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    files = three(src)
    dst.mkdir()
    (dst / files[0].name).write_bytes(files[0].read_bytes())                              # already there, same content
    (dst / files[1].name).write_bytes(b"different")                                       # same name, other content
    with pytest.raises(BackupError, match="different content"):
        backup.move_backups(src, dst)
    assert not files[0].exists()                                                          # the identical one was just cleaned up
    assert files[1].exists() and files[2].exists() and (dst / files[1].name).read_bytes() == b"different"


def test_symlinks_are_never_followed_or_moved(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    secret = tmp_path / "secret.bin"; secret.write_bytes(b"x" * 10)
    (src / "laminator-backup-link.tar.gz").symlink_to(secret)
    assert backup.move_backups(src, dst) == [] and secret.exists() and not list(dst.glob("*backup*"))


# ---- the web UI -----------------------------------------------------------------------------------------------------
def test_settings_shows_the_folder_and_status_mentions_it(ui):
    app, c, tok, root = ui
    page = c.get("/settings").text
    assert "Backup folder" in page and str((root / "backups").resolve()) in page.replace("&#39;", "'") or str(root / "backups") in page
    assert "the default, inside the data folder" in page
    assert "Backup folder" in c.get("/status").text and str(root / "backups") in c.get("/status").text.replace("&#39;", "'")


def test_saving_a_folder_validates_it_and_new_backups_go_there(ui, tmp_path):
    app, c, tok, root = ui
    post = lambda **kw: c.post("/settings/backup/location", data={"token": tok(), **kw})
    for bad in ("relative", "/a/../b", str(root), str(root / "servers" / "emby")):
        assert "note=bkbad" in post(path=bad).headers["location"], bad
    assert "backup_dir" not in config.load_settings(root)
    dest = tmp_path / "other-disk"
    assert "note=bkloc" in post(path=str(dest)).headers["location"]
    assert config.load_settings(root)["backup_dir"] == str(dest) and dest.is_dir()
    page = c.get("/settings").text
    assert f"In use: <code>{dest}</code>" in page
    log = finished(app, c, make(c, tok).headers["location"])
    assert "done" in log and len(list(dest.glob("laminator-backup-*.tar.gz"))) == 1 and not list((root / "backups").glob("*.tar.gz"))
    name = next(dest.glob("*.tar.gz")).name
    assert c.get(f"/backups/download/{name}").status_code == 200                           # download, verify and restore read the same folder
    assert name in c.get("/settings").text
    assert "note=bklocdefault" in post(path="").headers["location"]
    assert "backup_dir" not in config.load_settings(root)


def test_an_unwritable_folder_is_refused_with_a_message(ui, tmp_path):
    app, c, tok, root = ui
    f = tmp_path / "afile"; f.write_text("x")
    r = c.post("/settings/backup/location", data={"token": tok(), "path": str(f / "sub")})
    assert "note=bkbad" in r.headers["location"] and "backup_dir" not in config.load_settings(root)


def test_changing_the_folder_can_move_the_existing_backups_as_a_job(ui, tmp_path):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    old_files = list((root / "backups").glob("*.tar.gz"))
    assert len(old_files) == 1
    dest = tmp_path / "moved-here"
    r = c.post("/settings/backup/location", data={"token": tok(), "path": str(dest), "move": "1"})
    assert r.headers["location"].startswith("/jobs/")
    log = finished(app, c, r.headers["location"])
    assert "moved to" in log and "done: 1 backup(s) moved" in log
    assert [f.name for f in dest.glob("*.tar.gz")] == [old_files[0].name] and not list((root / "backups").glob("*.tar.gz"))
    assert backup.verify(dest / old_files[0].name).files > 0


def test_backups_left_in_the_default_folder_can_be_moved_later(ui, tmp_path):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    dest = tmp_path / "later"
    assert "note=bkloc" in c.post("/settings/backup/location", data={"token": tok(), "path": str(dest)}).headers["location"]   # move box not ticked
    page = c.get("/settings").text
    assert "still in the default folder" in page and "Move it to the folder in use" in page
    r = c.post("/settings/backup/move", data={"token": tok()})
    log = finished(app, c, r.headers["location"])
    assert "done: 1 backup(s) moved" in log and len(list(dest.glob("*.tar.gz"))) == 1
    assert "still in the default folder" not in c.get("/settings").text
    assert "note=bkmoved" in c.post("/settings/backup/move", data={"token": tok()}).headers["location"]


def test_the_folder_routes_need_csrf(ui):
    app, c, tok, root = ui
    assert c.post("/settings/backup/location", data={"path": "/x", "token": ""}).status_code == 403
    assert c.post("/settings/backup/move", data={"token": ""}).status_code == 403


def test_uploads_land_in_the_chosen_folder(ui, tmp_path):
    app, c, tok, root = ui
    finished(app, c, make(c, tok).headers["location"])
    f = next((root / "backups").glob("*.tar.gz"))
    dest = tmp_path / "uploads-go-here"
    c.post("/settings/backup/location", data={"token": tok(), "path": str(dest)})
    r = c.post("/backups/upload", data={"token": tok()}, files={"file": ("b.tar.gz", f.read_bytes(), "application/gzip")})
    assert "note=bkup" in r.headers["location"] and len(list(dest.glob("laminator-backup-uploaded-*.tar.gz"))) == 1


def test_the_shown_paths_are_absolute_even_when_the_data_folder_is_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backups").mkdir()
    (tmp_path / "backups" / "laminator-backup-20261101-010101.tar.gz").write_bytes(b"x")
    loc = backup.location_info(Path("."))
    assert loc["path"] == str((tmp_path / "backups").resolve()) == loc["default"] and loc["count"] == 1 and loc["ok"]
    s = config.load_settings(Path(".")); s["backup_dir"] = str(tmp_path / "elsewhere"); config.save_settings(Path("."), s)
    loc = backup.location_info(Path("."))
    assert loc["path"] == str(tmp_path / "elsewhere") and loc["default"] == str((tmp_path / "backups").resolve()) and loc["stranded"] == 1
