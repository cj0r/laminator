import io
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator.web import auth
from laminator.web.app import create_app

SECRET = "SUPER-SECRET-API-KEY"


def jpeg(w=200, h=300):
    b = io.BytesIO(); Image.new("RGB", (w, h), (90, 120, 160)).save(b, "JPEG"); return b.getvalue()


class FakeServer:
    api_key = SECRET  # must never appear in any response

    def __init__(self):
        self.items_ = [NS(id="1", name="Elf", type="Movie", year=2003, provider_ids={"imdb": "tt1"}, has_poster=True, poster_tag="t1"),
                       NS(id="2", name='<script>alert(1)</script>', type="Movie", year=1999, provider_ids={}, has_poster=True, poster_tag="t2")]

    def libraries(self): return [{"Name": "Movies", "CollectionType": "movies"}]
    def items(self, lib=None, limit=None): return self.items_
    def get_full(self, i): return {"OfficialRating": "PG", "CriticRating": None, "LockedFields": []}
    def download_primary(self, i, max_width=None): return jpeg(), "image/jpeg"
    def ping(self): return "Fake Emby 4.10"


@pytest.fixture
def web(tmp_path):
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", FakeMdb())
    return app, TestClient(app, follow_redirects=False)


def login(c):
    r = c.post("/login", data={"password": "pw"})
    assert r.status_code == 303 and auth.COOKIE in r.cookies
    return r


def test_everything_needs_login_except_health_and_static(web):
    _, c = web
    assert c.get("/healthz").status_code == 200
    assert c.get("/static/style.css").status_code == 200
    for path in ("/", "/library", "/item/1", "/templates", "/status", "/img/1/current"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/login", path


def test_login_wrong_right_and_rate_limit(web):
    _, c = web
    for _ in range(5):
        assert c.post("/login", data={"password": "nope"}).status_code == 401
    assert c.post("/login", data={"password": "pw"}).status_code == 429  # even the right one is blocked for a while


def test_pages_render_after_login_and_escape_untrusted_names(web):
    _, c = web
    login(c)
    for path in ("/library", "/item/1", "/templates", "/status"):
        assert c.get(path).status_code == 200, path
    html = c.get("/library").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    part = c.get("/library", headers={"HX-Request": "true"}).text
    assert "<html" not in part and "<p" in part  # a partial, no <html>
    assert "Elf" in c.get("/library?q=elf").text and "Elf" not in c.get("/library?q=zzz").text


def test_images_are_jpegs_and_params_are_clamped(web):
    _, c = web
    login(c)
    for url in ("/img/1/current", "/img/1/original", "/img/1/preview?score=8.5", "/img/1/preview?sz=99&gh=-5&font=../../etc/passwd&em=abc"):
        r = c.get(url)
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg", url
        Image.open(io.BytesIO(r.content)).verify()
    assert c.get("/img/999/current").status_code == 404
    assert c.get("/img/1/bogus").status_code == 404


def test_template_panel_returns_yaml_without_saving(web, tmp_path):
    _, c = web
    login(c)
    r = c.get("/templates/panel?name=default&item=1&sz=0.08")
    assert r.status_code == 200 and "size: 0.08" in r.text
    assert not (tmp_path / "templates").exists()


def test_no_secret_in_any_response_and_security_headers(web):
    _, c = web
    login(c)
    for path in ("/library", "/item/1", "/templates", "/status", "/templates/panel?name=default&item=1"):
        r = c.get(path)
        assert SECRET not in r.text
        assert r.headers["x-content-type-options"] == "nosniff" and "default-src 'self'" in r.headers["content-security-policy"]


def test_ui_is_read_only(web):
    app, _ = web
    writers = {(r.path, m) for r in app.routes if hasattr(r, "methods") for m in r.methods
               if m not in ("GET", "HEAD", "OPTIONS")}
    assert writers == {("/login", "POST"), ("/logout", "POST"), ("/jobs/start", "POST"), ("/jobs/{job_id}/cancel", "POST"), ("/settings/key", "POST"),
                       ("/settings/clear", "POST"), ("/settings/test", "POST"), ("/settings/password", "POST"),
                       ("/settings/password/reset", "POST"), ("/settings/server", "POST"), ("/settings/reset", "POST"),
                       ("/settings/general", "POST"), ("/settings/backup/create", "POST"), ("/backups/verify", "POST"),
                       ("/backups/upload", "POST"), ("/backups/restore", "POST"), ("/metadata/edit", "POST"),
                       ("/metadata/edit/undo", "POST"), ("/metadata/add", "POST"), ("/metadata/apply", "POST"), ("/posters/use", "POST"), ("/history/use", "POST"), ("/export/create", "POST"), ("/cleanup/run", "POST"), ("/trash/restore", "POST"), ("/settings/destination", "POST"), ("/templates/save", "POST"), ("/login/code", "POST"), ("/setup", "POST"), ("/settings/mfa/start", "POST"), ("/settings/mfa/confirm", "POST"), ("/settings/mfa/disable", "POST"), ("/settings/mfa/recovery", "POST"), ("/settings/sessions/end", "POST"), ("/templates/yaml", "POST"), ("/assets/upload", "POST"), ("/assets/remove", "POST"), ("/assets/apply", "POST"), ("/settings/assets", "POST"), ("/templates/delete", "POST"), ("/settings/backup/schedule", "POST"), ("/settings/destination/test", "POST")}


def test_session_tokens(tmp_path):
    s = auth.load_secret(tmp_path)
    assert auth.load_secret(tmp_path) == s and (tmp_path / ".secret").stat().st_mode & 0o077 == 0
    t = auth.sign(s, 2_000_000_000)
    assert auth.valid(s, t, now=1.0) and not auth.valid(s, t, now=2_000_000_001)
    assert not auth.valid(s, t[:-1] + ("0" if t[-1] != "0" else "1")) and not auth.valid(s, "x") and not auth.valid(b"other", t, now=1.0)


# ---- phase 2: running things --------------------------------------------------------------------------
import re

from laminator import actions
from laminator.actions import Event


def token_of(client):
    html = client.get("/confirm?action=apply&scope=item&item=1").text
    return re.search(r'name="token" value="([0-9a-f]+)"', html).group(1)


@pytest.fixture
def runnable(web, monkeypatch):
    app, c = web
    calls = []

    def fake_apply(ws, tpl, sig, overrides_path, required=False, item=None, critic=False, only_changed=False):
        calls.append(("apply", ws.scfg.library, item, critic) + (("changed",) if only_changed else ()))
        yield Event("apply", "Elf", "aggregate 7.3", "1")

    def fake_restore_posters(ws, item=None):
        calls.append(("restore_posters", item))
        yield Event("restore", "Elf")

    def fake_restore_meta(ws, item=None, force=False):
        calls.append(("restore_meta", item))
        return iter(())

    monkeypatch.setattr(actions, "apply_posters", fake_apply)
    monkeypatch.setattr(actions, "restore_posters", fake_restore_posters)
    monkeypatch.setattr(actions, "restore_metadata", fake_restore_meta)
    login(c)
    return app, c, calls


def post(c, **kw):
    base = {"action": "apply", "scope": "item", "item": "1", "lib": "", "what": "all", "template": "default", "phrase": "", "token": token_of(c), "srv": "emby"}
    return c.post("/jobs/start", data={**base, **kw})


def finished(app, c, location):
    jid = location.rsplit("/", 1)[1]
    for _ in range(100):
        if "running" not in c.get(f"/jobs/{jid}/log").text:
            return c.get(f"/jobs/{jid}/log").text
        import time; time.sleep(0.02)
    raise AssertionError("job did not finish")


def test_apply_item_runs_in_background_and_shows_the_log(runnable):
    app, c, calls = runnable
    r = post(c)
    assert r.status_code == 303 and r.headers["location"].startswith("/jobs/")
    log = finished(app, c, r.headers["location"])
    assert "apply  Elf: aggregate 7.3" in log and "done" in log
    assert calls == [("apply", None, "1", False)]


def test_apply_library_and_restore_scopes(runnable):
    app, c, calls = runnable
    finished(app, c, post(c, scope="library", lib="Movies").headers["location"])
    finished(app, c, post(c, action="restore", what="all").headers["location"])
    assert ("apply", "Movies", None, False) in calls
    assert ("restore_meta", "1") in calls and ("restore_posters", "1") in calls


def test_everything_needs_the_typed_phrase(runnable):
    app, c, calls = runnable
    assert post(c, scope="all", phrase="yes").status_code == 400
    assert calls == []
    r = post(c, scope="all", phrase="EVERYTHING")
    assert r.status_code == 303
    finished(app, c, r.headers["location"])
    assert calls == [("apply", None, None, False)]


def test_csrf_and_origin_are_enforced(runnable):
    app, c, calls = runnable
    assert post(c, token="").status_code == 403
    assert post(c, token="0" * 64).status_code == 403
    assert c.post("/jobs/start", data={"action": "apply", "scope": "item", "item": "1", "token": token_of(c)},
                  headers={"origin": "http://evil.example"}).status_code == 403
    assert calls == []


def test_bad_input_is_rejected(runnable):
    app, c, calls = runnable
    assert post(c, action="delete").status_code == 400
    assert post(c, scope="library", action="restore", lib="Movies").status_code == 400   # restore has no library scope
    assert post(c, scope="item", item="999").status_code == 404
    assert post(c, scope="library", lib="Nope").status_code == 404
    assert post(c, template="../../etc/passwd").status_code == 404
    assert calls == []


def test_only_one_job_at_a_time(runnable, monkeypatch):
    app, c, calls = runnable
    import threading
    gate = threading.Event()

    def slow(ws, tpl, sig, overrides_path, required=False, item=None, critic=False, only_changed=False):
        gate.wait(5)
        yield Event("apply", "Elf", "x", "1")

    monkeypatch.setattr(actions, "apply_posters", slow)
    first = post(c)
    assert first.status_code == 303
    second = post(c)                                       # not refused: it waits in line and starts by itself
    assert second.status_code == 303 and second.headers["location"] != first.headers["location"]
    assert "waiting" in c.get(second.headers["location"]).text and "queued behind" in c.get(second.headers["location"]).text
    gate.set()
    finished(app, c, first.headers["location"])
    finished(app, c, second.headers["location"])


def test_a_failing_job_reports_instead_of_crashing(runnable, monkeypatch):
    app, c, calls = runnable

    def boom(*a, **k):
        raise RuntimeError("server on fire")
        yield

    monkeypatch.setattr(actions, "apply_posters", boom)
    log = finished(app, c, post(c).headers["location"])
    assert "server on fire" in log and "with problems" in log
    assert c.get("/library").status_code == 200   # the UI is still up


def test_without_a_password_the_ui_is_read_only(tmp_path):
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, None)
    c = TestClient(app, follow_redirects=False)
    assert c.get("/library").status_code == 200
    assert "Apply" not in c.get("/item/1").text and "read-only" in c.get("/library").text
    assert c.get("/confirm?action=apply&scope=item&item=1").status_code == 403
    assert c.post("/jobs/start", data={"action": "apply", "scope": "item", "item": "1", "token": "x"}).status_code == 403


def test_pages_show_write_buttons_when_allowed(runnable):
    app, c, calls = runnable
    assert "Apply overlay" in c.get("/item/1").text
    assert "Apply to this library" in c.get("/library").text
    assert "Apply to everything" in c.get("/jobs").text


def test_changed_only_checkbox_is_offered_and_passed_through(runnable):
    app, c, calls = runnable
    assert 'name="changed"' in c.get("/confirm?action=apply&scope=library&lib=Movies").text
    assert 'name="changed"' not in c.get("/confirm?action=apply&scope=item&item=1").text
    finished(app, c, post(c, scope="library", lib="Movies", changed="1").headers["location"])
    assert ("apply", "Movies", None, False, "changed") in calls


def test_status_shows_the_schedule_and_maintenance_link_unticks_the_box(tmp_path):
    from laminator import schedule as sm
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", None, sm.Schedule(changed_every_minutes=30, maintenance_day=1))
    c = TestClient(app, follow_redirects=False)
    login(c)
    html = c.get("/status").text
    assert "check for new or changed posters: every 30 min" in html and "monthly on day 1 at 03:00" in html
    assert app.state.scheduler is not None and not app.state.scheduler.is_alive()      # not started unless asked
    assert "checked" in c.get("/confirm?action=apply&scope=library&lib=Movies").text.split('name="changed"')[1][:40]
    assert "checked" not in c.get("/confirm?action=apply&scope=all&changed=0").text.split('name="changed"')[1][:40]
    assert "Run full maintenance now" in c.get("/jobs").text


def test_no_scheduler_without_a_password_or_without_a_schedule(tmp_path):
    from laminator import schedule as sm
    assert create_app({}, FakeServer(), tmp_path, None, None, sm.Schedule()).state.scheduler is None   # read-only mode never writes
    assert create_app({}, FakeServer(), tmp_path, "pw").state.scheduler is None


# ---- settings: API keys ---------------------------------------------------------------------------------
import os

import httpx

from laminator import secrets_store

KEY = "SECRETKEY-abcdef0123456789"


class FakeMdb:
    remaining = None
    def __init__(self, ok=True): self.ok = ok
    def lookup(self, *args): return None
    def check(self):
        if not self.ok:
            raise httpx.HTTPStatusError("x", request=None, response=NS(status_code=403))
        return {"plan": "free", "used": 5, "limit": 1000, "remaining": 995}


@pytest.fixture
def settings_ui(tmp_path, monkeypatch):
    for n in ("API_EMBY", "API_MDBLIST"):
        monkeypatch.delenv(n, raising=False)       # also registers cleanup of whatever the app sets
    monkeypatch.setattr(secrets_store, "_UNDER", {})
    built = []

    def rebuild():
        built.append(os.environ.get("API_EMBY"))
        return FakeServer(), FakeMdb()

    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", FakeMdb(), rebuild=rebuild)
    c = TestClient(app, follow_redirects=False)
    login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path, built


def test_saving_a_key_stores_it_privately_reconnects_and_never_echoes_it(settings_ui):
    app, c, tok, data, built = settings_ui
    r = c.post("/settings/key", data={"name": "API_EMBY", "value": KEY, "token": tok()})
    assert r.status_code == 303 and "note=ok" in r.headers["location"]
    assert KEY not in r.headers["location"]                                   # not in the redirect either
    assert built == [KEY]                                                      # the connection was rebuilt with it
    f = data / "secrets.env"
    assert oct(f.stat().st_mode & 0o777) == "0o600" and f"API_EMBY={KEY}" in f.read_text()
    page = c.get("/settings").text
    assert KEY not in page and "•" in page                                    # masked
    assert KEY[-4:] in page and "key saved" in page
    for path in ("/status", "/library", "/settings", "/item/1"):
        assert KEY not in c.get(path).text


def test_bad_values_and_unknown_names_are_rejected_without_touching_anything(settings_ui):
    app, c, tok, data, built = settings_ui
    for bad in ("short", "has space in it 1234", "semi;colon;1234567", "a" * 300, "x=y\nAPI_EMBY=z1234567"):
        r = c.post("/settings/key", data={"name": "API_EMBY", "value": bad, "token": tok()})
        assert r.status_code == 303 and "note=bad" in r.headers["location"], bad
    assert not (data / "secrets.env").exists() and built == []
    assert c.post("/settings/key", data={"name": "PATH", "value": KEY, "token": tok()}).status_code == 400
    assert c.post("/settings/key", data={"name": "LAMINATOR_ADMIN_PASSWORD", "value": KEY, "token": tok()}).status_code == 400


def test_csrf_origin_and_read_only_rules_apply_to_settings(settings_ui, tmp_path):
    app, c, tok, data, built = settings_ui
    body = {"name": "API_EMBY", "value": KEY}
    assert c.post("/settings/key", data={**body, "token": ""}).status_code == 403
    assert c.post("/settings/key", data={**body, "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert not (data / "secrets.env").exists()
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app({}, FakeServer(), tmp_path / "ro", None), follow_redirects=False)   # no password: read-only
    assert ro.post("/settings/key", data={**body, "token": "x"}).status_code == 403
    assert "Paste the key" not in ro.get("/settings").text


def test_a_saved_key_overrides_the_environment_and_removing_it_restores_it(settings_ui, monkeypatch):
    app, c, tok, data, built = settings_ui
    monkeypatch.setenv("API_EMBY", "ENVKEY-0123456789abcdef")
    page = c.get("/settings").text
    assert "key from .env / environment" in page and "ENVKEY-0123456789abcdef" not in page
    assert 'name="api_key"' in page and "Leave blank to keep the current key" in page       # the server form takes a new key even when one comes from the environment
    r = c.post("/settings/key", data={"name": "API_EMBY", "value": KEY, "token": tok()})
    assert "note=ok" in r.headers["location"] and os.environ["API_EMBY"] == KEY and built == [KEY]
    page = c.get("/settings").text
    assert "key saved" in page and "overrides the one in .env" in page and KEY not in page
    r = c.post("/settings/clear", data={"name": "API_EMBY", "token": tok()})
    assert "note=cleared" in r.headers["location"]
    assert os.environ["API_EMBY"] == "ENVKEY-0123456789abcdef" and built == [KEY, "ENVKEY-0123456789abcdef"]   # reconnected with the old key
    assert "from .env / the environment" in c.get("/settings").text


def test_clear_and_test_buttons(settings_ui):
    app, c, tok, data, built = settings_ui
    c.post("/settings/key", data={"name": "API_MDBLIST", "value": KEY, "token": tok()})
    r = c.post("/settings/test", data={"name": "API_MDBLIST", "token": tok()})
    assert "note=ok" in r.headers["location"] and "995" in r.headers["location"]
    assert "Remove saved key" in c.get("/settings").text
    r = c.post("/settings/clear", data={"name": "API_MDBLIST", "token": tok()})
    assert "note=cleared" in r.headers["location"] and secrets_store.read(data) == {} and "API_MDBLIST" not in os.environ


def test_a_rejected_key_is_reported_without_echoing_it(settings_ui):
    app, c, tok, data, built = settings_ui
    app2 = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), data, "pw", FakeMdb(ok=False),
                      rebuild=lambda: (FakeServer(), FakeMdb(ok=False)))
    c3 = TestClient(app2, follow_redirects=False); login(c3)
    t = re.search(r'name="token" value="([0-9a-f]+)"', c3.get("/settings").text).group(1)
    r = c3.post("/settings/test", data={"name": "API_MDBLIST", "token": t})
    loc = r.headers["location"]
    assert "note=fail" in loc and "403" in loc and KEY not in loc


def test_the_ui_starts_without_any_keys_and_points_at_settings(tmp_path):
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, None, tmp_path, "pw", None)
    c = TestClient(app, follow_redirects=False); login(c)
    for path in ("/library", "/item/1", "/templates", "/confirm?action=apply&scope=all"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/settings?note=noserver", path
    assert c.get("/img/1/current").status_code == 503
    assert c.get("/settings").status_code == 200 and "not connected" in c.get("/settings").text
    assert c.get("/status").status_code == 200


def test_connection_status_is_checked_not_assumed(settings_ui):
    app, c, tok, data, built = settings_ui
    assert "connected to Fake Emby 4.10" in c.get("/settings").text
    class Rejecting(FakeServer):
        def ping(self): raise httpx.HTTPStatusError("x", request=None, response=NS(status_code=401))
    app2 = create_app({"server": {"kind": "emby", "url": "http://x"}}, Rejecting(), data, "pw", None)
    c2 = TestClient(app2, follow_redirects=False); login(c2)
    assert "the server rejected the key (HTTP 401)" in c2.get("/settings").text


# ---- the interface password -----------------------------------------------------------------------------
from laminator.web import passwords

NEW_PW = "a much better password"


@pytest.fixture
def pw_ui(tmp_path):
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw")
    c = TestClient(app, follow_redirects=False)
    login(c)
    tok = lambda cl=None: re.search(r'name="token" value="([0-9a-f]+)"', (cl or c).get("/settings").text).group(1)
    change = lambda cl=None, **kw: (cl or c).post("/settings/password", data={"current": "pw", "new": NEW_PW, "confirm": NEW_PW,
                                                                              "token": tok(cl), **kw})
    return app, c, tok, change, tmp_path


def test_changing_the_password_hashes_it_signs_out_others_and_keeps_this_session(pw_ui):
    app, c, tok, change, data = pw_ui
    other = TestClient(app, follow_redirects=False); login(other)          # a second browser, logged in
    assert other.get("/library").status_code == 200
    r = change()
    assert r.status_code == 303 and "note=pwok" in r.headers["location"]
    stored = (data / "admin.hash").read_text()
    assert NEW_PW not in stored and stored.startswith("scrypt$") and oct((data / "admin.hash").stat().st_mode & 0o777) == "0o600"
    assert c.get("/library").status_code == 200                           # this browser got a fresh cookie
    assert other.get("/library").status_code == 303                       # the other session is out
    fresh = TestClient(app, follow_redirects=False)
    assert fresh.post("/login", data={"password": "pw"}).status_code == 401  # the old (environment) password is dead
    assert fresh.post("/login", data={"password": NEW_PW}).status_code == 303
    assert NEW_PW not in c.get("/settings").text and "saved in settings" in c.get("/settings").text


def test_wrong_current_mismatch_and_weak_passwords_change_nothing(pw_ui):
    app, c, tok, change, data = pw_ui
    assert "note=pwwrong" in change(current="nope").headers["location"]
    assert "note=pwmismatch" in change(confirm="different one here").headers["location"]
    r = change(new="short", confirm="short")
    assert "note=bad" in r.headers["location"] and "at+least+10" in r.headers["location"]
    assert not (data / "admin.hash").exists()
    assert TestClient(app).post("/login", data={"password": "pw"}, follow_redirects=False).status_code == 303   # unchanged


def test_guessing_the_current_password_is_rate_limited(pw_ui):
    app, c, tok, change, data = pw_ui
    for _ in range(5):
        assert "note=pwwrong" in change(current="guess").headers["location"]
    assert "note=pwlimit" in change(current="guess").headers["location"]
    assert "note=pwlimit" in change().headers["location"]                 # even the right one waits
    assert not (data / "admin.hash").exists()


def test_password_changes_need_csrf_origin_and_a_password(pw_ui, tmp_path):
    app, c, tok, change, data = pw_ui
    assert change(token="").status_code == 403
    assert c.post("/settings/password", data={"current": "pw", "new": NEW_PW, "confirm": NEW_PW, "token": tok()},
                  headers={"origin": "http://evil.example"}).status_code == 403
    assert not (data / "admin.hash").exists()
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app({}, FakeServer(), tmp_path / "ro", None), follow_redirects=False)
    assert ro.post("/settings/password", data={"current": "", "new": NEW_PW, "confirm": NEW_PW, "token": "x"}).status_code == 403


def test_removing_the_saved_password_goes_back_to_the_environment_one(pw_ui):
    app, c, tok, change, data = pw_ui
    change()
    r = c.post("/settings/password/reset", data={"token": tok()})
    assert "note=pwreset" in r.headers["location"] and not (data / "admin.hash").exists()
    fresh = TestClient(app, follow_redirects=False)
    assert fresh.post("/login", data={"password": "pw"}).status_code == 303 and fresh.post("/login", data={"password": NEW_PW}).status_code == 401
    assert c.get("/library").status_code == 200                           # still signed in here


def test_cannot_remove_the_saved_password_when_nothing_is_underneath(tmp_path):
    passwords.save(tmp_path, NEW_PW)                                       # UI-only password, no environment one
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, None)
    c = TestClient(app, follow_redirects=False)
    assert c.post("/login", data={"password": NEW_PW}).status_code == 303
    tok = re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    assert "note=pwnoreset" in c.post("/settings/password/reset", data={"token": tok}).headers["location"]
    assert (tmp_path / "admin.hash").exists()
    assert "Remove the saved password" not in c.get("/settings").text


def test_a_saved_password_alone_is_enough_to_start_with_auth_and_writes(tmp_path):
    passwords.save(tmp_path, NEW_PW)
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, None)   # no env password at all
    c = TestClient(app, follow_redirects=False)
    assert c.get("/library").status_code == 303                           # login required
    assert c.post("/login", data={"password": "admin"}).status_code == 401
    assert c.post("/login", data={"password": NEW_PW}).status_code == 303 and "Apply overlay" in c.get("/item/1").text


def test_writing_the_score_to_criticrating_is_off_by_default_and_needs_an_explicit_choice(tmp_path):
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", FakeMdb())          # no critic_rating anywhere
    c = TestClient(app, follow_redirects=False); login(c)
    page = c.get("/settings").text
    assert re.search(r'<input type="checkbox" name="critic" value="1"\s*>', page)                                    # present, not checked
    assert "Off by default" in page and "off" in c.get("/status").text.split("Write score to CriticRating")[1][:60]
    tok = re.search(r'name="token" value="([0-9a-f]+)"', page).group(1)
    body = {"changed_every_minutes": "30", "maint_on": "1", "maint_day": "1", "maint_time": "03:00", "sched_template": "default",
            "grace_days": "30", "trash_days": "90", "token": tok}
    c.post("/settings/general", data=body)                                                                            # saving other options keeps it off
    assert 'name="critic" value="1" checked' not in c.get("/settings").text.replace("\n", " ") and 'name="critic" value="1"  checked' not in c.get("/settings").text
    c.post("/settings/general", data={**body, "critic": "1"})
    assert re.search(r'name="critic" value="1"\s+checked', c.get("/settings").text)


def test_save_test_and_remove_share_one_row_on_the_server_card_and_still_work(settings_ui):
    app, c, tok, data, built = settings_ui
    c.post("/settings/key", data={"name": "API_EMBY", "value": KEY, "token": tok()})
    page = c.get("/settings").text
    form = re.search(r'<form method="post" action="/settings/server".*?</form>', page, re.S).group(0)
    row = re.search(r'<div class="row"><button class="btn">Save Emby</button>(.*?)</form>', form, re.S).group(1)
    assert 'formaction="/settings/test"' in row and 'formaction="/settings/clear"' in row                       # side by side, one row
    # the whole server form is what gets posted by those buttons: the extra fields must be ignored
    posted = {"kind": "emby", "url": "http://x", "library": "", "enabled": "1", "api_key": "", "name": "API_EMBY", "token": tok()}
    r = c.post("/settings/test", data=posted)
    assert r.status_code == 303 and "note=ok" in r.headers["location"]
    r = c.post("/settings/clear", data=posted)
    assert r.status_code == 303 and "API_EMBY" not in os.environ and not (data / "secrets.env").read_text().count("API_EMBY=")


def test_the_footer_offers_the_ways_to_support_it_and_no_link_to_the_unpublished_repository(web):
    _, c = web
    login(c)
    page = c.get("/library").text
    for href in ("https://www.buymeacoffee.com/cj0r", "https://ko-fi.com/cj000r"):
        assert f'href="{href}"' in page, href
    assert "github.com" not in page and "GitHub" not in page                                                # the repository is not public yet
    assert page.count('rel="noopener noreferrer"') == 2 and page.count('target="_blank"') == 2             # new tab, no access to this page
    assert "Buy Me A Coffee" in page and "Ko-fi" in page and "Support development" in page
    assert "https://ko-fi.com/cj000r" in c.get("/settings").text                                            # on every page, not just the library


def test_a_dev_build_shows_its_commit_after_the_version(tmp_path, monkeypatch):
    from laminator import __version__
    monkeypatch.setenv("LAMINATOR_BUILD", "a1b2c3d")
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    assert f'v{__version__}-dev+a1b2c3d' in c.get("/library").text
    monkeypatch.delenv("LAMINATOR_BUILD")
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, FakeServer(), tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    page = c.get("/library").text
    assert f'v{__version__}<' in page and "-dev+" not in page


def test_the_version_is_visible_on_every_kind_of_page_including_phones(web):
    from laminator import __version__
    app, c = web
    assert f"v{__version__}" in c.get("/login").text                                    # before signing in
    login(c)
    for path in ("/library", "/status", "/settings"):
        assert f"laminator v{__version__}" in c.get(path).text, path                  # in the footer, which phones do not hide (the header badge is hidden below 760 px)
