import os
import socket
from pathlib import Path

import pytest

from laminator import destinations as D
from laminator.backup import AUTO_PREFIX

A = lambda n: f"{AUTO_PREFIX}202611{n:02d}-120000.tar.gz"


def mk(tmp_path, name, data=b"x" * 5000):
    f = tmp_path / "src" / name
    f.parent.mkdir(exist_ok=True)
    f.write_bytes(data)
    return f


def test_validate_accepts_good_and_rejects_bad_input():
    assert D.validate({"type": "none"}) == {"type": "none"}
    assert D.validate({"type": "local", "path": "/backups/"}) == {"type": "local", "path": "/backups"}
    ok = D.validate({"type": "smb", "host": "nas.local", "port": "445", "share": "Backups", "folder": "/laminator/x/", "username": "me"})
    assert ok["folder"] == "laminator/x" and ok["port"] == 445 and "password" not in ok
    for bad in ({"type": "ftp"}, {"type": "local", "path": "rel"}, {"type": "local", "path": "/a/../b"}, {"type": "local", "path": "/a\x00"},
                {"type": "smb", "host": "bad host;", "share": "S", "username": "u"}, {"type": "smb", "host": "h", "share": "a/b", "username": "u"},
                {"type": "smb", "host": "h", "share": "S", "username": "u", "folder": "../x"}, {"type": "smb", "host": "h", "share": "S", "username": "u", "port": "0"},
                {"type": "smb", "host": "h", "share": "S", "username": "u", "port": "x"}, {"type": "smb", "host": "h", "share": "S", "username": ""},
                {"type": "smb", "host": "h", "share": "S", "username": "u", "domain": "a b"}):
        with pytest.raises(ValueError):
            D.validate(bad)


def test_local_copy_is_verified_atomic_and_prunes_only_scheduled_backups(tmp_path):
    dest = tmp_path / "share"; dest.mkdir()
    d = D.LocalDest(dest)
    (dest / "holiday-photos.tar.gz").write_text("not mine")
    (dest / "laminator-backup-20260101-000000.tar.gz").write_text("manual")
    for n in (1, 2, 3, 4):
        D.push(mk(tmp_path, A(n)), d, keep=2)
    assert sorted(p.name for p in dest.iterdir()) == ["holiday-photos.tar.gz", "laminator-backup-20260101-000000.tar.gz", A(3), A(4)]
    assert not list(dest.glob("*.part")) and (dest / A(4)).read_bytes() == b"x" * 5000
    D.push(mk(tmp_path, "laminator-backup-20261105-000000.tar.gz"), d, keep=1)   # a manual backup is copied but never prunes
    assert (dest / A(3)).exists() and (dest / A(4)).exists()


def test_a_copy_that_does_not_match_is_not_kept(tmp_path, monkeypatch):
    dest = tmp_path / "share"; dest.mkdir()
    real = D._sha_file
    monkeypatch.setattr(D, "_sha_file", lambda p: "bad" if p.suffix == ".part" else real(p))
    with pytest.raises(D.DestError):
        D.push(mk(tmp_path, A(1)), D.LocalDest(dest))
    assert list(dest.iterdir()) == []


def test_local_errors_are_sentences(tmp_path):
    with pytest.raises(D.DestError, match="volume mounted"):
        D.LocalDest(tmp_path / "nope").check()
    ro = tmp_path / "ro"; ro.mkdir(); ro.chmod(0o500)
    try:
        if os.geteuid() != 0:
            with pytest.raises(D.DestError, match="cannot write"):
                D.LocalDest(ro).check()
    finally:
        ro.chmod(0o700)
    assert "writable" in D.LocalDest(tmp_path).check()


def test_symlinks_are_never_deleted_or_listed(tmp_path):
    dest = tmp_path / "share"; dest.mkdir(); outside = tmp_path / "keep.txt"; outside.write_text("x")
    (dest / A(1)).symlink_to(outside)
    d = D.LocalDest(dest)
    assert d.names() == [] and (d.delete(A(1)) is None) and outside.exists() and (dest / A(1)).is_symlink()


def test_from_cfg_takes_the_smb_password_from_the_environment_only():
    cfg = {"backup_destination": {"type": "smb", "host": "h", "share": "S", "username": "u", "password": "in-config"}}
    d = D.from_cfg(cfg, {D.SMB_PASSWORD_ENV: "from-env"})
    assert d.password == "from-env" and "S" in d.describe() and "from-env" not in d.describe()
    assert D.from_cfg({}) is None and D.from_cfg({"backup_destination": {"type": "none"}}) is None
    assert D.from_cfg({"backup_destination": {"type": "smb", "host": "bad;host"}}) is None      # a damaged setting never takes backups down


def test_smb_errors_never_contain_the_password():
    d = D.SmbDest("127.0.0.1", "S", "f", "u", "SuperSecret!123", 1)
    with pytest.raises(D.DestError) as e:
        d.check()
    assert "SuperSecret" not in str(e.value) and "could not reach" in str(e.value)


def _smb_up():
    try:
        socket.create_connection(("127.0.0.1", 1445), 0.5).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _smb_up(), reason="needs the throwaway samba container on 127.0.0.1:1445")
def test_real_smb_round_trip(tmp_path):
    d = D.SmbDest("127.0.0.1", "Backups", "pytest/run", "testuser", "testpass-12345", 1445)
    d.check()
    for n in (1, 2, 3):
        D.push(mk(tmp_path, A(n), os.urandom(200_000)), d, keep=2)
    assert sorted(d.names()) == [A(2), A(3)]
    with pytest.raises(D.DestError, match="rejected"):
        D.SmbDest("127.0.0.1", "Backups", "x", "testuser", "wrong", 1445).check()
    for n in d.names():
        d.delete(n)
