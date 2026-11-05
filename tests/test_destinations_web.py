import os

from test_backup_web import ui, make, finished  # noqa: F401


def save(c, tok, **kw):
    return c.post("/settings/destination", data={"token": tok(), **kw})


def test_destination_is_saved_validated_and_used_by_a_manual_backup(ui, tmp_path):
    app, c, tok, root = ui
    share = tmp_path / "share"; share.mkdir()
    assert "Second copy" in c.get("/settings").text
    r = save(c, tok, type="local", path="relative/oops")
    assert r.status_code == 303 and "note=bad" in r.headers["location"] and not (root / "settings.yaml").exists() or "backup_destination" not in (root / "settings.yaml").read_text()
    assert save(c, tok, type="local", path=str(share)).status_code == 303
    assert str(share) in c.get("/settings").text
    log = finished(app, c, make(c, tok).headers["location"])
    assert "copied to folder" in log and len(list(share.glob("laminator-backup-*.tar.gz"))) == 1
    t = c.post("/settings/destination/test", data={"token": tok()})
    assert "note=ok" in t.headers["location"] and not list(share.glob(".laminator-test-*"))


def test_smb_password_is_stored_masked_and_blank_keeps_it(ui):
    app, c, tok, root = ui
    f = dict(type="smb", host="nas.local", share="Backups", username="me", folder="laminator")
    save(c, tok, password="Sup3r secret!", **f)
    page = c.get("/settings").text
    assert "Sup3r" not in page and "saved; leave blank" in page
    save(c, tok, password="", **f)
    assert os.environ["LAMINATOR_SMB_PASSWORD"] == "Sup3r secret!"
    assert "Sup3r" not in (root / "settings.yaml").read_text()          # the password never goes into settings.yaml
    c.post("/settings/reset", data={"token": tok(), "section": "destination"})
    assert "LAMINATOR_SMB_PASSWORD" not in os.environ or not os.environ["LAMINATOR_SMB_PASSWORD"] == "Sup3r secret!"


def test_destination_routes_need_csrf(ui):
    app, c, tok, root = ui
    assert c.post("/settings/destination", data={"type": "none", "token": ""}).status_code == 403
    assert c.post("/settings/destination/test", data={"token": ""}).status_code == 403


def test_going_back_to_the_config_file_asks_first_and_explains(ui):
    app, c, tok, root = ui
    save(c, tok, type="local", path=str(root))
    page = c.get("/settings").text
    assert 'href="/settings/reset?section=destination"' in page and 'formaction="/settings/reset"' not in page      # no one-click reset any more
    ask = c.get("/settings/reset?section=destination")
    assert ask.status_code == 200 and "Remove the second backup copy?" in ask.text and "Backups already copied there stay" in ask.text
    assert "saved in settings" not in c.get("/settings").text and "backup_destination" in (root / "settings.yaml").read_text()   # asking changed nothing
    assert c.get("/settings/reset?section=general").status_code == 200 and "config.local.yaml" in c.get("/settings/reset?section=general").text
    assert c.get("/settings/reset?section=server:emby").status_code == 200
    assert c.get("/settings/reset?section=bogus").status_code == 400 and c.get("/settings/reset?section=server:plex").status_code == 400
    r = c.post("/settings/reset", data={"section": "destination", "token": tok()})                                  # only the confirmed POST resets
    assert "note=reset" in r.headers["location"] and "backup_destination" not in (root / "settings.yaml").read_text()
