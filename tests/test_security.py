import json
import os
import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from laminator import audit, cli
from laminator.config import ConfigError, load_secret_files
from laminator.web import auth, mfa, sessions
from laminator.web.app import create_app
from test_web import FakeServer

CFG = {"server": {"kind": "emby", "url": "http://x"}}


def app_for(tmp_path, **env):
    return create_app(dict(CFG), FakeServer(), tmp_path, "pw-long-enough")


def client(app, ip="198.51.100.7"):
    return TestClient(app, follow_redirects=False, client=(ip, 50000))


def sign_in(c, password="pw-long-enough"):
    return c.post("/login", data={"password": password})


def token(c):
    return re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)


def totp_now(secret):
    return mfa.code_at(secret, int(time.time() // mfa.STEP))


def next_code(data, secret):
    """A code laminator has not seen yet: the one after the last accepted step (each code works once)."""
    last = mfa.load(data).get("last_step", int(time.time() // mfa.STEP) - 1)
    return mfa.code_at(secret, last + 1)


# ---- sessions --------------------------------------------------------------------------------------------------------------
def test_logout_really_ends_the_session_so_a_copied_cookie_stops_working(tmp_path):
    c = client(app_for(tmp_path))
    sign_in(c)
    stolen = c.cookies.get(auth.COOKIE)
    assert c.get("/library").status_code == 200
    assert c.post("/logout", data={"token": "nope"}).status_code == 403                                   # logout needs the form token too
    assert c.post("/logout", data={"token": token(c)}).status_code == 303
    other = client(c.app)
    other.cookies.set(auth.COOKIE, stolen)
    assert other.get("/library").status_code == 303                                                       # the old cookie is dead on the server


def test_the_sessions_file_keeps_only_hashes_and_is_private(tmp_path):
    c = client(app_for(tmp_path))
    sign_in(c)
    raw = (tmp_path / "sessions.json").read_text()
    assert c.cookies.get(auth.COOKIE) not in raw and (tmp_path / "sessions.json").stat().st_mode & 0o077 == 0


def test_sessions_end_after_idle_time_and_after_the_absolute_limit(tmp_path):
    s = sessions.SessionStore(tmp_path)
    t0 = 1_000_000.0
    a = s.create(now=t0)
    assert s.check(a, now=t0 + 60)
    assert not s.check(a, now=t0 + 60 + sessions.IDLE + 1)                                               # idle too long
    b = s.create(now=t0)
    for i in range(1, 12):                                                                                # kept busy every hour
        assert s.check(b, now=t0 + i * 3600)
    assert not s.check(b, now=t0 + sessions.ABSOLUTE + 1)                                                 # but never past 12 hours
    assert not s.check("x" * 500) and not s.check(None)


def test_changing_the_password_ends_every_other_session_but_keeps_this_one(tmp_path):
    app = app_for(tmp_path)
    me, other = client(app), client(app, "198.51.100.8")
    sign_in(me); sign_in(other)
    r = me.post("/settings/password", data={"current": "pw-long-enough", "new": "a-new-password-1", "confirm": "a-new-password-1", "token": token(me)})
    assert "note=pwok" in r.headers["location"]
    assert me.get("/library").status_code == 200 and other.get("/library").status_code == 303


def test_sign_out_all_other_sessions(tmp_path):
    app = app_for(tmp_path)
    me, other = client(app), client(app, "198.51.100.8")
    sign_in(me); sign_in(other)
    assert "2 signed-in sessions" in me.get("/settings").text
    r = me.post("/settings/sessions/end", data={"token": token(me)})
    assert "Signed+out+1+other+session" in r.headers["location"]
    assert me.get("/library").status_code == 200 and other.get("/library").status_code == 303


# ---- two-factor sign-in ---------------------------------------------------------------------------------------------------------
def test_totp_matches_rfc_6238_and_a_code_works_once():
    import base64
    sec = base64.b32encode(b"12345678901234567890").decode()
    assert [mfa.code_at(sec, t // 30) for t in (59, 1111111109, 1234567890)] == ["287082", "081804", "005924"]
    step = mfa.match(sec, "287082", 59)
    assert step == 1 and mfa.match(sec, "287082", 59, last_step=step) is None and mfa.match(sec, "28708", 59) is None


def enrol(c, tmp_path):
    r = c.post("/settings/mfa/start", data={"current": "pw-long-enough", "token": token(c)})
    assert r.status_code == 303
    page = c.get("/settings/mfa").text
    assert 'src="data:image/svg+xml;base64,' in page                                                       # the QR code, CSP-safe
    sec = mfa.pending(tmp_path)
    assert c.post("/settings/mfa/confirm", data={"code": "000000" if totp_now(sec) != "000000" else "111111", "token": token(c)}).headers["location"].endswith("bad=code")
    r = c.post("/settings/mfa/confirm", data={"code": totp_now(sec), "token": token(c)})
    codes = re.findall(r"<code>([a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4})</code>", r.text)
    assert r.status_code == 200 and len(codes) == 10 and r.headers["cache-control"] == "no-store"
    return sec, codes


def test_setting_up_two_factor_needs_the_password_and_a_working_code(tmp_path):
    c = client(app_for(tmp_path))
    sign_in(c)
    assert "note=pwwrong" in c.post("/settings/mfa/start", data={"current": "wrong", "token": token(c)}).headers["location"]
    assert not mfa.enabled(tmp_path)
    sec, codes = enrol(c, tmp_path)
    assert mfa.enabled(tmp_path) and (tmp_path / "mfa.json").stat().st_mode & 0o077 == 0
    stored = (tmp_path / "mfa.json").read_text()
    assert not any(code in stored for code in codes)                                                       # only hashes of recovery codes


def test_signing_in_takes_the_password_then_a_code_and_cannot_skip_the_second_step(tmp_path):
    app = app_for(tmp_path)
    c = client(app)
    sign_in(c)
    sec, codes = enrol(c, tmp_path)
    d = client(app, "198.51.100.9")
    r = sign_in(d)
    assert r.headers["location"] == "/login/code" and auth.COOKIE not in r.cookies
    assert d.get("/library").status_code == 303                                                            # the password alone is not a session
    assert d.post("/login/code", data={"code": "123456" if totp_now(sec) != "123456" else "654321"}).status_code == 401
    good = next_code(tmp_path, sec)
    r = d.post("/login/code", data={"code": good})
    assert r.status_code == 303 and r.headers["location"] == "/library" and d.get("/library").status_code == 200
    e = client(app, "198.51.100.10")
    sign_in(e)
    assert e.post("/login/code", data={"code": good}).status_code == 401                                     # the same code cannot be used twice


def test_a_recovery_code_works_exactly_once(tmp_path):
    app = app_for(tmp_path)
    c = client(app)
    sign_in(c)
    sec, codes = enrol(c, tmp_path)
    for n, expect in ((1, 303), (2, 401)):
        d = client(app, f"198.51.100.{20 + n}")
        sign_in(d)
        r = d.post("/login/code", data={"code": codes[0].upper()})
        assert r.status_code == expect, n
    assert mfa.recovery_left(tmp_path) == 9


def test_the_second_step_gives_up_after_five_wrong_codes_and_a_forged_step_is_refused(tmp_path):
    app = app_for(tmp_path)
    c = client(app)
    sign_in(c)
    sec, _ = enrol(c, tmp_path)
    d = client(app, "198.51.100.30")
    sign_in(d)
    wrong = "000000" if totp_now(sec) != "000000" else "111111"
    for _ in range(4):
        assert d.post("/login/code", data={"code": wrong}).status_code == 401
    assert d.post("/login/code", data={"code": wrong}).status_code == 303                                   # back to the start
    assert d.post("/login/code", data={"code": totp_now(sec)}).headers["location"] == "/login"            # that step is spent
    f = client(app, "198.51.100.31")
    f.cookies.set("laminator_mfa", f"{int(time.time()) + 300}.abc.{'0' * 64}")
    assert f.get("/login/code").status_code == 303 and f.post("/login/code", data={"code": totp_now(sec)}).headers["location"] == "/login"


def test_turning_it_off_needs_the_password_and_a_code_and_reset_mfa_is_the_way_back_in(tmp_path, monkeypatch, capsys):
    app = app_for(tmp_path)
    c = client(app)
    sign_in(c)
    sec, codes = enrol(c, tmp_path)
    assert c.post("/settings/mfa/disable", data={"current": "pw-long-enough", "code": "", "token": token(c)}).headers["location"].endswith("bad=code")
    assert mfa.enabled(tmp_path)
    assert "note=mfaoff" in c.post("/settings/mfa/disable", data={"current": "pw-long-enough", "code": codes[1], "token": token(c)}).headers["location"]
    assert not mfa.enabled(tmp_path)
    mfa.start(tmp_path); mfa.confirm(tmp_path, totp_now(mfa.pending(tmp_path)))
    cfg = tmp_path / "c.yaml"; cfg.write_text(f"data_dir: {tmp_path}\n")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-c", str(cfg), "reset-mfa"]) == 0 and "is off" in capsys.readouterr().out
    assert not mfa.enabled(tmp_path) and not (tmp_path / "sessions.json").exists()


# ---- reverse proxies ------------------------------------------------------------------------------------------------------------
def test_forwarded_headers_count_only_from_a_trusted_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("LAMINATOR_TRUSTED_PROXIES", "10.0.0.2")
    app = app_for(tmp_path)
    proxy, direct = client(app, "10.0.0.2"), client(app, "198.51.100.40")
    for _ in range(5):
        proxy.post("/login", data={"password": "wrong"}, headers={"x-forwarded-for": "203.0.113.5"})
    assert proxy.post("/login", data={"password": "pw-long-enough"}, headers={"x-forwarded-for": "203.0.113.5"}).status_code == 429
    assert proxy.post("/login", data={"password": "pw-long-enough"}, headers={"x-forwarded-for": "203.0.113.6"}).status_code == 303   # another visitor is not locked out
    for _ in range(5):
        direct.post("/login", data={"password": "wrong"}, headers={"x-forwarded-for": f"203.0.113.{_}"})          # a faked header from outside
    assert direct.post("/login", data={"password": "pw-long-enough"}, headers={"x-forwarded-for": "203.0.113.99"}).status_code == 429


def test_secure_cookie_over_https_and_the_proxy_host_or_public_url_pass_the_origin_check(tmp_path, monkeypatch):
    monkeypatch.setenv("LAMINATOR_TRUSTED_PROXIES", "10.0.0.0/24")
    monkeypatch.setenv("LAMINATOR_PUBLIC_URL", "https://laminator.example.com")
    app = app_for(tmp_path)
    r = client(app, "10.0.0.2").post("/login", data={"password": "pw-long-enough"}, headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower()                                                    # HTTPS through the trusted proxy
    r = client(app, "198.51.100.60").post("/login", data={"password": "pw-long-enough"}, headers={"x-forwarded-proto": "https"})
    assert "secure" not in r.headers["set-cookie"].lower()                                                # a stranger's header is not believed
    p = client(app, "10.0.0.2")
    sign_in(p)                                                                                            # plain http on the inside
    tok = token(p)
    ok = p.post("/settings/sessions/end", data={"token": tok}, headers={"origin": "https://laminator.example.com", "host": "laminator:5264"})
    assert ok.status_code == 303                                                                          # nginx's default Host plus the public URL
    viaxfh = p.post("/settings/sessions/end", data={"token": tok}, headers={"origin": "https://other.example", "host": "laminator:5264", "x-forwarded-host": "other.example"})
    assert viaxfh.status_code == 303
    bad = p.post("/settings/sessions/end", data={"token": tok}, headers={"origin": "https://evil.example", "host": "laminator:5264"})
    assert bad.status_code == 403
    d = client(app, "198.51.100.50")
    sign_in(d)
    spoof = d.post("/settings/sessions/end", data={"token": token(d)}, headers={"origin": "https://evil.example", "x-forwarded-host": "evil.example"})
    assert spoof.status_code == 403                                                                       # X-Forwarded-Host from a stranger is ignored


def test_a_bad_trusted_proxies_value_stops_the_start(tmp_path, monkeypatch):
    monkeypatch.setenv("LAMINATOR_TRUSTED_PROXIES", "not-an-address")
    with pytest.raises(ValueError):
        app_for(tmp_path)


# ---- headers, files, secrets, the log -------------------------------------------------------------------------------------------
def test_security_headers_are_on_every_response(tmp_path):
    c = client(app_for(tmp_path))
    for r in (c.get("/login"), c.get("/library"), c.get("/static/style.css")):
        csp = r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp and "base-uri 'none'" in csp and "form-action 'self'" in csp
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff" and "permissions-policy" in r.headers
    assert c.get("/login").headers["cache-control"] == "no-store"


def test_secret_files_feed_the_environment_without_overriding_it(tmp_path):
    (tmp_path / "pw").write_text("from-a-file\n")
    env = {"LAMINATOR_ADMIN_PASSWORD_FILE": str(tmp_path / "pw"), "API_EMBY": "already", "API_EMBY_FILE": str(tmp_path / "pw")}
    assert load_secret_files(env) == ["LAMINATOR_ADMIN_PASSWORD"] and env["LAMINATOR_ADMIN_PASSWORD"] == "from-a-file" and env["API_EMBY"] == "already"
    with pytest.raises(ConfigError, match="cannot read"):
        load_secret_files({"API_MDBLIST_FILE": str(tmp_path / "missing")})


def test_old_world_readable_files_are_made_private_and_nothing_else_is_touched(tmp_path):
    (tmp_path / "settings.yaml").write_text("x"); (tmp_path / "settings.yaml").chmod(0o644)
    (tmp_path / "servers" / "emby").mkdir(parents=True); (tmp_path / "servers" / "emby" / "state.db").write_text("x")
    (tmp_path / "servers" / "emby" / "state.db").chmod(0o644)
    (tmp_path / "src").mkdir(); (tmp_path / "src" / "code.py").write_text("x"); (tmp_path / "src" / "code.py").chmod(0o644)
    assert cli.tighten_permissions(tmp_path) >= 2
    assert (tmp_path / "settings.yaml").stat().st_mode & 0o777 == 0o600 and (tmp_path / "servers" / "emby" / "state.db").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "servers").stat().st_mode & 0o777 == 0o700 and (tmp_path / "src" / "code.py").stat().st_mode & 0o777 == 0o644


def test_the_signing_key_is_created_private(tmp_path):
    auth.load_secret(tmp_path)
    assert (tmp_path / ".secret").stat().st_mode & 0o077 == 0


def test_the_security_log_records_sign_ins_without_secrets_and_shows_on_status(tmp_path):
    c = client(app_for(tmp_path))
    c.post("/login", data={"password": "a-wrong-guess"})
    sign_in(c)
    log = (tmp_path / "audit.log").read_text()
    assert "wrong password" in log and "signed in" in log and "a-wrong-guess" not in log and "pw-long-enough" not in log
    assert c.cookies.get(auth.COOKIE) not in log and (tmp_path / "audit.log").stat().st_mode & 0o077 == 0
    page = c.get("/status").text
    assert "Security log" in page and "wrong password" in page and "198.51.100.7" in page and "Two-factor sign-in" in page


# ---- first start: choosing the password -----------------------------------------------------------------------------------
def fresh_app(tmp_path):
    from laminator import schedule as schedmod
    return create_app(dict(CFG), FakeServer(), tmp_path, None, sched=schedmod.Schedule(), setup=True)


def test_a_first_start_offers_only_the_setup_page_until_a_password_exists(tmp_path):
    app = fresh_app(tmp_path)
    c = client(app)
    code = app.state.setup_code
    assert re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}", code)
    for path in ("/", "/library", "/settings", "/status", "/login", "/jobs", "/img/1/current"):
        r = c.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/setup", path
    assert c.get("/healthz").status_code == 200 and c.get("/static/style.css").status_code == 200 and c.get("/setup").status_code == 200
    assert c.post("/settings/key", data={"name": "API_EMBY", "value": "x" * 16, "token": "t"}).headers["location"] == "/setup"   # nothing writes
    assert not (tmp_path / "admin.hash").exists()


def test_the_setup_needs_the_code_from_the_log_and_a_good_password(tmp_path):
    app = fresh_app(tmp_path)
    c = client(app)
    code = app.state.setup_code
    pw = "a-first-password"
    assert c.post("/setup", data={"code": "WRONG-CODE", "password": pw, "confirm": pw}).status_code == 401
    assert c.post("/setup", data={"code": "", "password": pw, "confirm": pw}).status_code == 401
    assert c.post("/setup", data={"code": code, "password": pw, "confirm": "different"}).status_code == 422
    assert c.post("/setup", data={"code": code, "password": "short", "confirm": "short"}).status_code == 422
    assert not (tmp_path / "admin.hash").exists() and c.get("/library").status_code == 303                 # nothing was claimed
    r = c.post("/setup", data={"code": code.lower().replace("-", " "), "password": pw, "confirm": pw})    # case and dash do not matter
    assert r.status_code == 303 and r.headers["location"] == "/settings?note=setupdone" and auth.COOKIE in r.cookies


def test_the_password_is_kept_only_as_a_private_hash_and_the_app_then_works_normally(tmp_path):
    app = fresh_app(tmp_path)
    c = client(app)
    pw = "a-first-password"
    c.post("/setup", data={"code": app.state.setup_code, "password": pw, "confirm": pw})
    stored = (tmp_path / "admin.hash").read_text()
    assert stored.startswith("scrypt$") and pw not in stored and (tmp_path / "admin.hash").stat().st_mode & 0o077 == 0
    assert c.get("/library").status_code == 200 and "Password saved and you are signed in" in c.get("/settings?note=setupdone").text   # signed in already
    other = client(app, "198.51.100.77")
    assert other.get("/library").status_code == 303 and other.get("/library").headers["location"] == "/login"       # others need the password now
    assert other.post("/login", data={"password": "wrong"}).status_code == 401 and other.post("/login", data={"password": pw}).status_code == 303
    assert c.get("/setup").headers["location"] == "/login" or c.get("/setup").status_code == 303                    # the page is gone for good
    assert other.post("/setup", data={"code": app.state.setup_code, "password": "x" * 12, "confirm": "x" * 12}).status_code == 303
    assert other.post("/login", data={"password": "x" * 12}).status_code == 401                                     # a second claim changes nothing
    log = (tmp_path / "audit.log").read_text()
    assert "password chosen (first start)" in log and pw not in log


def test_wrong_codes_are_rate_limited_and_logged_without_the_code(tmp_path):
    app = fresh_app(tmp_path)
    c = client(app, "198.51.100.88")
    for _ in range(5):
        assert c.post("/setup", data={"code": "AAAA-BBBB", "password": "a-first-password", "confirm": "a-first-password"}).status_code == 401
    assert c.post("/setup", data={"code": app.state.setup_code, "password": "a-first-password", "confirm": "a-first-password"}).status_code == 429
    log = (tmp_path / "audit.log").read_text()
    assert log.count("wrong setup code") == 5 and "AAAA-BBBB" not in log and app.state.setup_code not in log


def test_an_existing_password_or_the_insecure_mode_never_shows_the_setup_page(tmp_path):
    app = create_app(dict(CFG), FakeServer(), tmp_path, "pw-long-enough", setup=True)                         # a password from the environment
    assert app.state.setup_code == "" and client(app).get("/setup").headers["location"] == "/login"
    ro = create_app(dict(CFG), FakeServer(), tmp_path / "ro", None) if (tmp_path / "ro").mkdir() is None else None
    assert ro.state.setup_code == "" and client(ro).get("/library").status_code == 200                       # the read-only local mode is unchanged


def test_the_scheduler_starts_after_the_password_is_chosen(tmp_path):
    app = fresh_app(tmp_path)
    assert app.state.scheduler is None
    c = client(app)
    c.post("/setup", data={"code": app.state.setup_code, "password": "a-first-password", "confirm": "a-first-password"})
    assert app.state.scheduler is not None
