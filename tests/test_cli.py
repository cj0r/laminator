import re
"""Smoke tests that actually execute cli.main(): wiring mistakes (a missing import, a renamed function) show up here."""
import pytest

from laminator import cli


def cfg_file(tmp_path, extra=""):
    f = tmp_path / "c.yaml"
    f.write_text(f"data_dir: {tmp_path / 'data'}\nserver:\n  kind: emby\n  url: http://127.0.0.1:9\n{extra}")
    return str(f)


def test_every_subcommand_parses():
    p = cli.build_parser()
    for argv in (["scan"], ["render"], ["apply", "--all", "--changed"], ["maintain"], ["restore", "--all"], ["meta", "diff"],
                 ["meta", "apply", "--force"], ["serve", "--no-schedule"]):
        assert p.parse_args(argv).fn


def test_main_runs_and_reports_errors_cleanly(tmp_path, capsys, monkeypatch):
    for n in ("API_EMBY", "API_MDBLIST", "LAMINATOR_ADMIN_PASSWORD"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.chdir(tmp_path)                                  # so no real .env is picked up
    assert cli.main(["-c", str(tmp_path / "missing.yaml"), "scan"]) == 2
    assert "config file not found" in capsys.readouterr().err
    assert cli.main(["-c", cfg_file(tmp_path), "scan"]) == 2     # no key
    assert "set API_EMBY" in capsys.readouterr().err
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    assert cli.main(["-c", cfg_file(tmp_path), "serve"]) == 0    # no password: starts in first-start mode instead of refusing
    out = capsys.readouterr().out
    assert "FIRST START" in out and re.search(r"[A-Z0-9]{4}-[A-Z0-9]{4}", out)
    assert cli.main(["-c", cfg_file(tmp_path), "serve", "--insecure-no-auth", "--host", "0.0.0.0"]) == 2
    assert cli.main(["-c", cfg_file(tmp_path), "apply"]) == 2    # scope guard (fails on the missing key first, still a clean 2)


def test_keys_saved_in_the_ui_are_visible_to_the_cli(tmp_path, capsys, monkeypatch):
    from laminator import secrets_store
    monkeypatch.delenv("API_EMBY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    secrets_store.save(tmp_path / "data", "API_EMBY", "abcdef0123456789abcdef")
    monkeypatch.delenv("API_EMBY")                                # as if a fresh process: only the file has it
    rc = cli.main(["-c", cfg_file(tmp_path), "scan", "--list-libraries"])
    err = capsys.readouterr().err
    assert "set API_EMBY" not in err and rc == 1 and "server error" in err   # got past the key check; nothing listens on port 9


def test_a_saved_key_overrides_the_environment_at_startup_and_clear_restores_it(tmp_path, monkeypatch):
    from laminator import secrets_store as ss
    monkeypatch.setattr(ss, "_UNDER", {})
    monkeypatch.setenv("API_EMBY", "ENVKEY-0123456789abcdef")
    ss.save(tmp_path, "API_EMBY", "SAVED-0123456789abcdef")
    monkeypatch.setattr(ss, "_UNDER", {})                              # as if a new process: only the files exist
    monkeypatch.setenv("API_EMBY", "ENVKEY-0123456789abcdef")
    ss.load_into_env(tmp_path)
    import os
    assert os.environ["API_EMBY"] == "SAVED-0123456789abcdef" and ss.overrides_environment(tmp_path, "API_EMBY")
    ss.clear(tmp_path, "API_EMBY")
    assert os.environ["API_EMBY"] == "ENVKEY-0123456789abcdef" and not ss.overrides_environment(tmp_path, "API_EMBY")
    ss.save(tmp_path, "API_MDBLIST", "ONLYSAVED-0123456789")           # nothing underneath: clearing removes it entirely
    monkeypatch.delenv("API_MDBLIST", raising=False)
    ss.clear(tmp_path, "API_MDBLIST")
    assert "API_MDBLIST" not in os.environ


def test_reset_password_command_and_serve_accepts_a_saved_password(tmp_path, capsys, monkeypatch):
    import uvicorn
    from laminator.web import passwords
    for n in ("API_EMBY", "API_MDBLIST", "LAMINATOR_ADMIN_PASSWORD"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.chdir(tmp_path)
    cfg = cfg_file(tmp_path)
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    assert cli.main(["-c", cfg, "serve"]) == 0 and "FIRST START" in capsys.readouterr().out   # nothing set: first-start mode with a setup code
    passwords.save(tmp_path / "data", "a saved password")
    assert cli.main(["-c", cfg, "serve"]) == 0                                   # the saved password is enough to start
    out = capsys.readouterr().out
    assert "set them up under Settings" in out and "FIRST START" not in out                  # no setup code once a password exists
    assert cli.main(["-c", cfg, "reset-password"]) == 0
    assert passwords.load(tmp_path / "data") is None and "Removed the saved UI password" in capsys.readouterr().out
    assert cli.main(["-c", cfg, "serve"]) == 0 and "FIRST START" in capsys.readouterr().out   # back to first-start mode
    assert cli.main(["-c", cfg, "reset-password"]) == 0 and "nothing to do" in capsys.readouterr().out


def test_backup_commands_round_trip(tmp_path, monkeypatch, capsys):
    from test_backup import make_root
    root = make_root(tmp_path / "data")
    cfg = tmp_path / "c.yaml"; cfg.write_text(f"data_dir: {root}\nserver: {{kind: emby, url: 'http://x'}}\n")
    for n in ("API_EMBY", "LAMINATOR_BACKUP_PASSPHRASE"):
        monkeypatch.delenv(n, raising=False)
    assert cli.main(["-c", str(cfg), "backup", "create"]) == 0
    out = capsys.readouterr().out
    assert "emby: 2 poster record(s)" in out and "not included" in out and "--include-secrets" in out
    f = next((root / "backups").glob("*.tar.gz"))
    assert cli.main(["-c", str(cfg), "backup", "list"]) == 0 and f.name in capsys.readouterr().out
    assert cli.main(["-c", str(cfg), "backup", "verify", str(f)]) == 0 and "OK:" in capsys.readouterr().out
    assert cli.main(["-c", str(cfg), "backup", "restore", str(f)]) == 2                      # data is there: refused without --force
    assert "--force" in capsys.readouterr().err
    assert cli.main(["-c", str(cfg), "backup", "restore", str(f), "--force", "--dry-run"]) == 0 and "dry run" in capsys.readouterr().out
    assert cli.main(["-c", str(cfg), "backup", "restore", str(f), "--force"]) == 0 and list(root.glob("pre-restore-*"))
    # secrets need a passphrase, taken from the environment (never from argv)
    assert cli.main(["-c", str(cfg), "backup", "create", "--include-secrets", "--out", str(tmp_path / "s.tar.gz")]) == 2     # no TTY, no env: refused
    monkeypatch.setenv("LAMINATOR_BACKUP_PASSPHRASE", "a long enough passphrase")
    assert cli.main(["-c", str(cfg), "backup", "create", "--include-secrets", "--out", str(tmp_path / "s.tar.gz")]) == 0
    assert cli.main(["-c", str(cfg), "backup", "verify", str(tmp_path / "s.tar.gz")]) == 0
    assert "decrypts" in capsys.readouterr().out
    bad = tmp_path / "junk.tar.gz"; bad.write_bytes(b"x")
    assert cli.main(["-c", str(cfg), "backup", "verify", str(bad)]) == 2 and "not a laminator backup" in capsys.readouterr().err


def test_cleanup_and_maintain_flags_parse_and_cleanup_defaults_to_a_plan_only():
    p = cli.build_parser()
    a = p.parse_args(["cleanup"])
    assert a.yes is False and a.safety_days is None
    assert p.parse_args(["cleanup", "--yes", "--safety-days", "180"]).safety_days == 180
    assert p.parse_args(["maintain", "--no-cleanup"]).no_cleanup is True and p.parse_args(["maintain"]).no_cleanup is False
    assert p.parse_args(["export", "--which", "original", "--layout", "library", "--naming", "stem", "--library", "Movies"]).naming == "stem"
    with pytest.raises(SystemExit):
        p.parse_args(["export", "--layout", "../.."])


def test_trash_and_auto_backup_commands(tmp_path, monkeypatch, capsys):
    from test_backup import make_root
    root = make_root(tmp_path / "data")
    cfg = tmp_path / "c.yaml"; cfg.write_text(f"data_dir: {root}\nserver: {{kind: emby, url: 'http://127.0.0.1:9'}}\n")
    for n in ("API_EMBY", "LAMINATOR_BACKUP_PASSPHRASE"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("API_EMBY", "e" * 16)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-c", str(cfg), "backup", "create", "--auto", "--keep", "2"]) == 0
    out = capsys.readouterr().out
    assert "laminator-backup-auto-" in out and "not included" in out
    assert cli.main(["-c", str(cfg), "backup", "create", "--auto", "--include-secrets"]) == 2       # scheduled backups never carry secrets
    assert "Do not combine" in capsys.readouterr().err
    assert cli.main(["-c", str(cfg), "trash", "list"]) == 0 and "the trash is empty" in capsys.readouterr().out
    assert cli.main(["-c", str(cfg), "trash", "restore", "20261101-000000"]) == 2                    # must say what to restore
    assert "--item ID" in capsys.readouterr().err
    assert cli.main(["-c", str(cfg), "trash", "restore", "../x", "--all"]) == 2                      # a bad batch name is a clean error
    assert "bad trash name" in capsys.readouterr().err
