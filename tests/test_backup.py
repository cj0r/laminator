import hashlib
import io
import json
import os
import sqlite3
import tarfile
from pathlib import Path

import pytest

from laminator import backup, store
from laminator.backup import BackupError


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def make_root(root: Path) -> Path:
    """A realistic data folder: two servers, state with absolute paths, settings, secrets, plus things that must NOT be backed up."""
    for kind, ids in (("emby", ("8", "9")), ("jellyfin", ("abc",))):
        d = root / "servers" / kind
        (d / "originals").mkdir(parents=True)
        (d / "out").mkdir()
        (d / "out" / "junk.jpg").write_bytes(b"rendered output")
        (d / "schedule.json").write_text('{"last_full": 1}')
        st = store.Store(d / "state.db")
        for i in ids:
            (d / "originals" / f"{i}.img").write_bytes(f"ORIGINAL-{kind}-{i}".encode() * 100)
            st.put_original(i, f"Item{i}", str(d / "originals" / f"{i}.img"), "sha" + i, "tag" + i)
        st.meta_put(ids[0], "Item", "critic_rating", None, 73, True)
        st.db.close()
    arc = root / "servers" / "emby" / "archive-20261001-120000"
    (arc / "originals").mkdir(parents=True)
    (arc / "originals" / "1.img").write_bytes(b"OLD ORIGINAL")
    (arc / "state.db").write_bytes(b"SQLite format 3\x00 archived")
    (root / "settings.yaml").write_text("servers:\n  emby: {url: 'http://x:8096'}\n")
    (root / "overrides.yaml").write_text("overrides: []\n")
    (root / "templates").mkdir(); (root / "templates" / "mine.yaml").write_text("sources: [imdb]\n")
    (root / "fonts").mkdir(); (root / "fonts" / "My Font.ttf").write_bytes(b"FONT")
    (root / "secrets.env").write_text("API_EMBY=EMBYKEY-0123456789abcdef\n")
    (root / "admin.hash").write_text("scrypt$16384$8$1$abc$def\n")
    (root / ".secret").write_bytes(b"session-signing-key")
    (root / "cache" / "mdblist").mkdir(parents=True); (root / "cache" / "mdblist" / "x.json").write_text("{}")
    return root


def names(path: Path) -> set[str]:
    with tarfile.open(path, "r:gz") as t:
        return {m.name for m in t}


def rewrite(path: Path, edit, extra=()) -> Path:
    """Copy an archive, letting `edit(name, data) -> (name, data) | None` change or drop members, and rebuild the manifest hashes
    only when asked by the edit (so tampering stays detectable)."""
    out = path.with_name("tampered.tar.gz")
    with tarfile.open(path, "r:gz") as src, tarfile.open(out, "w:gz") as dst:
        for m in src:
            data = src.extractfile(m).read()
            r = edit(m.name, data)
            if r is None:
                continue
            ti = tarfile.TarInfo(r[0]); ti.size = len(r[1])
            dst.addfile(ti, io.BytesIO(r[1]))
        for name, data in extra:                                                     # members the manifest does not know about
            ti = tarfile.TarInfo(name); ti.size = len(data)
            dst.addfile(ti, io.BytesIO(data))
    return out


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path / "data")


def test_a_backup_holds_what_it_should_and_nothing_else(root):
    rep = backup.create(root)
    got = names(rep.path)
    assert "manifest.json" in got and "servers/emby/state.db" in got and "servers/jellyfin/state.db" in got
    assert {"servers/emby/originals/8.img", "servers/emby/originals/9.img", "servers/jellyfin/originals/abc.img"} <= got
    assert "servers/emby/archive-20261001-120000/originals/1.img" in got      # archived old state is data too
    assert {"settings.yaml", "overrides.yaml", "templates/mine.yaml", "fonts/My Font.ttf", "servers/emby/schedule.json"} <= got
    for banned in ("secrets.env", "admin.hash", ".secret", "secrets.enc"):
        assert banned not in got                                                # secrets are opt-in, and the signing key never goes in
    assert not any(n.startswith(("cache", "out", "backups", ".")) or "/out/" in n for n in got)
    assert rep.servers["emby"] == {"posters": 2, "meta": 1, "originals": 2} and not rep.has_secrets
    assert oct(rep.path.stat().st_mode & 0o777) == "0o600"                      # private by default
    assert backup.verify(rep.path).files == len(got) - 1


def test_the_snapshot_is_consistent_even_when_the_live_database_is_in_wal_mode(root):
    live = store.Store(root / "servers" / "emby" / "state.db")
    live.db.execute("PRAGMA journal_mode=WAL")                                  # an older version's database: new rows may exist only in the -wal file
    live.put_original("777", "Fresh", str(root / "servers" / "emby" / "originals" / "8.img"), "s", "t")
    assert (root / "servers" / "emby" / "state.db-wal").exists()
    rep = backup.create(root)                                                   # while the connection is still open
    new = root.parent / "new"
    backup.restore(rep.path, new)
    con = sqlite3.connect(new / "servers" / "emby" / "state.db")
    assert "777" in {r[0] for r in con.execute("select item_id from posters")}  # a raw file copy would have lost it
    assert con.execute("pragma integrity_check").fetchone()[0] == "ok"


def test_restoring_into_a_fresh_folder_reproduces_everything_and_fixes_paths(root):
    rep = backup.create(root)
    new = root.parent / "elsewhere"
    r = backup.restore(rep.path, new)
    for rel in ("servers/emby/originals/8.img", "servers/jellyfin/originals/abc.img", "settings.yaml", "templates/mine.yaml",
                "fonts/My Font.ttf", "servers/emby/archive-20261001-120000/originals/1.img"):
        assert sha(new / rel) == sha(root / rel), rel
    for kind in ("emby", "jellyfin"):
        st = store.Store(new / "servers" / kind / "state.db")
        for row in st.all():
            p = Path(row["original_path"])
            assert p.exists() and p.is_relative_to(new.resolve()) and p.parent == (new / "servers" / kind / "originals").resolve()
    assert not (new / "secrets.env").exists() and not (new / "admin.hash").exists()
    assert r.has_secrets is False


def test_secrets_are_encrypted_and_only_restored_with_the_right_passphrase(root):
    with pytest.raises(BackupError, match="needs a passphrase"):
        backup.create(root, include_secrets=True)
    with pytest.raises(BackupError, match="at least"):
        backup.create(root, include_secrets=True, passphrase="short")
    rep = backup.create(root, include_secrets=True, passphrase="correct horse battery")
    raw = rep.path.read_bytes()
    assert b"EMBYKEY-0123456789abcdef" not in raw and b"scrypt$16384" not in raw           # not even inside the compressed stream
    blob = _member(rep.path, "secrets.enc")
    assert b"EMBYKEY" not in blob and blob.startswith(b"LMSEC1")
    new = root.parent / "new"
    with pytest.raises(BackupError, match="wrong passphrase"):
        backup.restore(rep.path, new, passphrase="not the passphrase")
    assert not new.exists() or not any(new.iterdir())                                      # a bad passphrase wrote NOTHING
    r = backup.restore(rep.path, new, passphrase="correct horse battery")
    assert (new / "secrets.env").read_text() == "API_EMBY=EMBYKEY-0123456789abcdef\n" and (new / "admin.hash").exists()
    assert oct((new / "secrets.env").stat().st_mode & 0o777) == "0o600" and "were restored" in " ".join(r.notes)
    new2 = root.parent / "new2"
    r2 = backup.restore(rep.path, new2)                                                     # no passphrase: everything but the secrets
    assert not (new2 / "secrets.env").exists() and (new2 / "settings.yaml").exists() and "NOT restored" in " ".join(r2.notes)


def _member(path: Path, name: str) -> bytes:
    with tarfile.open(path, "r:gz") as t:
        return t.extractfile(name).read()


def test_restore_never_overwrites_silently_and_a_forced_restore_moves_the_old_files_aside(root):
    rep = backup.create(root)
    with pytest.raises(BackupError, match="--force"):
        backup.restore(rep.path, root)                                                     # the folder it came from has data
    before = sha(root / "settings.yaml")
    d = backup.restore(rep.path, root, dry_run=True, force=True)
    assert "dry run" in " ".join(d.notes) and sha(root / "settings.yaml") == before and not list(root.glob("pre-restore-*"))
    (root / "settings.yaml").write_text("changed: after the backup\n")
    backup.restore(rep.path, root, force=True)
    assert sha(root / "settings.yaml") == before                                           # back to the backed-up version
    aside = next(root.glob("pre-restore-*"))
    assert (aside / "settings.yaml").read_text() == "changed: after the backup\n"           # the replaced file is kept, not deleted
    assert (aside / "servers" / "emby" / "state.db").exists()


def test_empty_folders_do_not_block_a_restore(root, tmp_path):
    rep = backup.create(root)
    fresh = tmp_path / "fresh"
    (fresh / "servers" / "emby").mkdir(parents=True)                                       # what any command leaves behind
    backup.restore(rep.path, fresh)
    assert (fresh / "servers" / "emby" / "state.db").exists() and not list(fresh.glob("pre-restore-*"))


def test_damage_is_detected_before_anything_is_written(root, tmp_path):
    rep = backup.create(root)
    bad = rewrite(rep.path, lambda n, d: (n, d + b"X") if n == "servers/emby/originals/8.img" else (n, d))
    with pytest.raises(BackupError, match="checksum mismatch"):
        backup.verify(bad)
    new = tmp_path / "new"
    with pytest.raises(BackupError):
        backup.restore(bad, new)
    assert not new.exists() or not any(new.iterdir())
    gone = rewrite(rep.path, lambda n, d: None if n == "servers/emby/originals/9.img" else (n, d))
    with pytest.raises(BackupError, match="missing"):
        backup.verify(gone)
    with pytest.raises(BackupError, match="not in its manifest"):                       # a stowaway with a legal name the manifest omits
        backup.verify(rewrite(rep.path, lambda n, d: (n, d), extra=[("templates/evil.yaml", b"x: 1")]))
    with pytest.raises(BackupError, match="not allowed"):                                # a member with an illegal name
        backup.verify(rewrite(rep.path, lambda n, d: (n, d), extra=[("run.sh", b"#!/bin/sh")]))


def test_hostile_archives_are_refused(tmp_path):
    def build(member_name, kind=tarfile.REGTYPE):
        p = tmp_path / f"evil-{abs(hash((member_name, kind)))}.tar.gz"
        data = b"pwned"
        manifest = {"format": 1, "created": "x", "servers": {}, "files": {member_name: {"sha256": hashlib.sha256(data).hexdigest(), "size": 5}}}
        with tarfile.open(p, "w:gz") as t:
            ti = tarfile.TarInfo(member_name); ti.type = kind; ti.size = len(data) if kind == tarfile.REGTYPE else 0
            if kind == tarfile.SYMTYPE:
                ti.linkname = "/etc/passwd"
            t.addfile(ti, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
            mb = json.dumps(manifest).encode(); mi = tarfile.TarInfo("manifest.json"); mi.size = len(mb)
            t.addfile(mi, io.BytesIO(mb))
        return p
    for name in ("../../etc/cron.d/x", "/etc/passwd", "servers/emby/../../x", "servers/plex/state.db", "settings.yaml/../x",
                 "servers/emby/originals/a\\b", ".env", "run.sh"):
        with pytest.raises(BackupError, match="not allowed"):
            backup.verify(build(name))
        with pytest.raises(BackupError):
            backup.restore(build(name), tmp_path / "t")
    with pytest.raises(BackupError, match="not a plain file"):
        backup.verify(build("settings.yaml", tarfile.SYMTYPE))
    assert not (tmp_path / "t").exists() or not any((tmp_path / "t").iterdir())
    junk = tmp_path / "not-a-backup.tar.gz"; junk.write_bytes(b"hello")
    with pytest.raises(BackupError, match="not a laminator backup"):
        backup.verify(junk)


def test_list_and_the_wrong_format_version(root):
    backup.create(root)
    rows = backup.list_backups(root)
    assert len(rows) == 1 and rows[0].servers["emby"]["posters"] == 2 and not rows[0].notes
    newer = rewrite(rows[0].path, lambda n, d: (n, json.dumps({**json.loads(d), "format": 99}).encode()) if n == "manifest.json" else (n, d))
    with pytest.raises(BackupError, match="unsupported backup format"):
        backup.verify(newer)


def test_encryption_round_trip_and_tampering():
    blob = backup.encrypt_secrets({"secrets.env": "A=1"}, "a long enough passphrase")
    assert backup.decrypt_secrets(blob, "a long enough passphrase") == {"secrets.env": "A=1"}
    assert backup.encrypt_secrets({"x": "y"}, "a long enough passphrase") != blob                # fresh salt + nonce every time
    with pytest.raises(BackupError, match="wrong passphrase"):
        backup.decrypt_secrets(blob, "a different passphrase")
    flipped = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(BackupError, match="wrong passphrase, or the secrets block is damaged"):
        backup.decrypt_secrets(flipped, "a long enough passphrase")
    with pytest.raises(BackupError, match="not a laminator secrets block"):
        backup.decrypt_secrets(b"garbage", "x" * 12)


# ---- listing must not unpack every archive on every page load ------------------------------------------------------------------
def test_listing_reads_each_backup_once_and_remembers_it(tmp_path, monkeypatch):
    root = make_root(tmp_path / "data")
    made = backup.create(root)                                  # created in the usual place: the index is primed
    calls = []
    real = backup.read_manifest
    monkeypatch.setattr(backup, "read_manifest", lambda f: calls.append(f.name) or real(f))
    first = backup.list_backups(root)
    assert calls == [] and [r.path.name for r in first] == [made.path.name]
    assert first[0].created == made.created and first[0].files == made.files and first[0].servers == made.servers and first[0].size == made.size
    (root / "cache" / "backup-index.json").unlink()             # lost index: rebuilt by reading once, then remembered
    assert backup.list_backups(root)[0].files == made.files and calls == [made.path.name]
    backup.list_backups(root)
    assert calls == [made.path.name]


def test_listing_notices_a_changed_or_removed_backup(tmp_path, monkeypatch):
    root = make_root(tmp_path / "data")
    a = backup.create(root)
    other = backup.backups_dir(root) / "laminator-backup-copy.tar.gz"
    other.write_bytes(a.path.read_bytes())
    assert len(backup.list_backups(root)) == 2
    other.write_bytes(b"not a backup")                           # replaced by something else: no stale summary
    rows = {r.path.name: r for r in backup.list_backups(root)}
    assert rows[other.name].notes and rows[other.name].created == "" and rows[a.path.name].created == a.created
    other.unlink()
    backup.list_backups(root)
    assert set(json.loads((root / "cache" / "backup-index.json").read_text())) == {a.path.name}   # nothing kept for deleted files
