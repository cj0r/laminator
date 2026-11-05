"""The web UI. The media servers' API keys never leave the process: the browser only sees images proxied through /img/...
Writes (POST) need a password, a CSRF token and a matching Origin. Several servers (one Emby, one Jellyfin) can be
connected at once; every page and every write says which one it is about."""
from __future__ import annotations

import copy
import hashlib
import hmac
import io
import json
import os
import secrets
import re
import threading
import weakref
import time
import unicodedata
from datetime import datetime
from decimal import Decimal
from shutil import copy2 as shutil_copy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlparse

import httpx
import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image

import ipaddress

from .. import actions, assets
from .. import audit
from .. import storage as storagemod
from .. import baseposters as bp
from .. import __version__
from .. import backup as bk
from .. import destinations as dst
from .. import cleanup as cln
from .. import trash as trs
from .. import maintenance
from .. import export as exp
from .. import posters as pst
from .. import meta as metamod
from .. import schedule as schedmod
from .. import secrets_store
from ..config import (KINDS, MDBLIST_ENV, ConfigError, ServerConfig, apply_settings, enabled_servers, load_server,
                      load_settings, normalize_url, save_settings, server_dir, server_key_env, servers, validate_library)
from .. import mdblist as mdbmod
from ..mdblist import MDBList, MDBListError
from ..render import render
from ..server import connect
from ..store import Store, archive_state, state_counts
from . import auth, mfa
from . import sessions as sessmod, passwords
from .jobs import JobManager
from .scheduler import Scheduler

HERE = Path(__file__).parent
PAGE = 48
COOKIE_VIEW = "laminator_view"
VIEWS = {"posters": ("Posters", 48), "compact": ("Compact", 120), "list": ("List", 100)}  # name -> (label, titles per page)
SHOW_GROUPS = [("Scores", [("no_mdb", "No MDBList score"), ("has_mdb", "MDBList score available"), ("my_score", "Manual score override")]),
               ("Poster", [("not_managed", "Not managed"), ("overlay", "Overlay applied"), ("original", "Original restored"),
                           ("changed", "Changed on server"), ("custom", "Custom poster"), ("no_poster", "No server poster"),
                           ("thumb", "Thumbnail instead of poster"), ("zero", "Badge reads 0.0")]),
               ("Metadata", [("meta_written", "Metadata overridden"), ("in_file", "Listed in overrides.yaml")])]
NO_RATING = "__none__"
SHOW_LABEL = {v: label for _, opts in SHOW_GROUPS for v, label in opts}
SELECTION_MAX = 5000  # the most titles one "apply to these" run takes
COOKIE_PER = "laminator_per"
PER_CHOICES = [("scroll", "Scroll (loads as you go)")] + [(str(n), f"{n} per page") for n in (25, 50, 75, 100, 250, 500)]
RAIL = ["#"] + [chr(c) for c in range(65, 91)]


def sort_key(name: str) -> tuple:
    """Alphabetical, accents folded (Elf, Émile, Fargo), digits and symbols first."""
    k = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower() or name.lower()
    return (k[:1].isalpha(), k)


def bucket(name: str) -> str:
    k = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().upper()
    return k[0] if k[:1].isalpha() else "#"
PHRASE = "EVERYTHING"
POSTER_WAIT_TRIES, POSTER_WAIT_STEP = 30, 1.0  # how long to wait for the server to switch to the image it was asked to fetch
COOKIE_SRV = "laminator_srv"
LABEL = {"emby": "Emby", "jellyfin": "Jellyfin"}
_NUM = re.compile(r"^-?\d+(\.\d+)?$")
EXAMPLE_OVERRIDES = '''# Metadata overrides. Each entry names an item (by imdb/tmdb/tvdb id, or library + title + year) and the fields to set.
# Fields: rating, sort_title, original_title, overview (written to the server and locked), score (the poster overlay only).
# Optional: server: emby|jellyfin to limit an entry to one server.
overrides:
'''
MAX_UPLOAD = int(os.environ.get("LAMINATOR_MAX_UPLOAD_MB", "4096")) * 1024 * 1024
NEEDS_SERVER = ("/assets", "/library", "/item/", "/templates", "/img/", "/confirm", "/jobs/start", "/posters", "/metadata", "/history", "/export", "/cleanup", "/trash")
PUBLIC = ("/login", "/healthz", "/static")
NOT_RESPONDING = ("<!doctype html><meta charset=utf-8><meta http-equiv=refresh content=15><title>laminator</title>"
                  "<body style=\"font:16px system-ui;max-width:34em;margin:4em auto;padding:0 1em\"><h1>The data folder is not responding</h1>"
                  "<p>laminator has not been able to read its data folder for {secs} seconds. The disk or filesystem under it is probably stuck, "
                  "not laminator. Nothing is lost; this page retries by itself and the interface comes back when the disk does.</p>"
                  "<p>Scheduled runs are paused until then. If it lasts, check the server's kernel log (<code>dmesg</code>).</p>")
STATE_TITLES = {"plain": "laminator has not changed this poster",
                "orig": "laminator saved the original, and the original (no score) is what the server shows now",
                "warn": "the poster in the server changed since laminator last wrote it; the next apply deals with it",
                "ok": "the score is drawn on the poster in the server"}
SOURCES = {"aggregate": "MDBList combined score", "imdb": "IMDb", "tmdb": "TMDb", "trakt": "Trakt", "letterboxd": "Letterboxd",
           "tomatoes": "Rotten Tomatoes (critics)", "popcorn": "Rotten Tomatoes (audience)", "metacritic": "Metacritic (critics)",
           "metacriticuser": "Metacritic (users)", "rogerebert": "Roger Ebert"}
SOURCE_ORDER = list(SOURCES)
assert SOURCE_ORDER == assets.SOURCE_IDS  # one list of providers, used for the dropdown and for checking edited YAML
TEMPLATE_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ASSET_NOTES = {"saved": ("ok", "Custom poster saved. It is drawn on in the run that just started."), "removed": ("ok", "Custom poster removed (kept in the trash folder). The item keeps its current base poster; use Poster history to go back to an earlier one."),
               "busy": ("warn", "Saved, but another run is working. The poster is used the next time a run handles this item."),
               "bad": ("bad", ""), "folder": ("ok", "Custom posters folder saved."), "nofile": ("warn", "That poster is no longer in the folder.")}
TPL_NOTES = {"saved": "Template saved.", "updated": "Template updated.", "reset": "Default template reset to the built-in look.", "deleted": "Template deleted.", "badname": "Give the template a name (letters and numbers).",
             "builtin": "That name belongs to the built-in template; choose another name.", "nodel": "Only templates you saved can be deleted.",
             "inuse": "That template is used by the scheduled runs (Settings); pick another there first."}
LIMITS = {"gh": (0.02, 0.4), "mo": (0.0, 1.0), "sz": (0.02, 0.12), "mx": (0.0, 0.15), "my": (0.0, 0.15), "em": (0.0, 0.06)}
NOTES = {"saved": ("ok", "Saved."), "destok": ("ok", "Saved the backup destination. Use \"Test it\" to check it works."), "cleared": ("ok", "Removed the saved key."),
         "noserver": ("warn", "No media server is connected yet: add its address and API key below."),
         "busy": ("warn", "A job is running. Try again when it has finished."), "bad": ("bad", "That does not look right."),
         "ok": ("ok", ""), "fail": ("bad", ""),
         "pwok": ("ok", "Password changed. Every other session was signed out."),
         "pwwrong": ("bad", "The current password is wrong."), "pwmismatch": ("bad", "The two new passwords do not match."),
         "pwlimit": ("bad", "Too many wrong attempts. Wait a few minutes."),
         "pwreset": ("ok", "Removed the saved password. The one in .env / the environment applies again; other sessions were signed out."),
         "pwnoreset": ("warn", "There is no password in .env or the environment to fall back to, so the saved one cannot be removed."),
         "srvsaved": ("ok", ""), "genok": ("ok", "Settings saved."), "reset": ("ok", "Back to the values in the config file."),
         "bkup": ("ok", "Backup uploaded and verified."), "setupdone": ("ok", "Password saved and you are signed in. Next: add your media server and your MDBList key below."), "mfaoff": ("ok", "Two-factor sign-in is off. Every other session was signed out."),
         "mfabad": ("bad", ""), "sessionsended": ("ok", ""), "mfarecovery": ("warn", "You signed in with a recovery code. It cannot be used again; make new ones under Interface password if you are running low."), "assetsok": ("ok", "Custom posters folder saved."), "bkbad": ("bad", ""),
         "mdadded": ("ok", "Override added to overrides.yaml. Review it below, then apply."), "mdsaved": ("ok", "overrides.yaml saved."),
         "mdentry": ("ok", "Override updated in overrides.yaml. Review the change below, then apply."),
         "mddup": ("warn", "This item already has an override. Edit it in the editor instead of adding a second one."),
         "mdundo": ("ok", "Restored the previous version of overrides.yaml."), "mdbad": ("bad", "")}


SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; object-src 'none'; "
                               "base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
MFA_COOKIE, MFA_TTL, MFA_TRIES = "laminator_mfa", 300, 5
MFA_ERRORS = {"code": "That code is not right. Check the time on your phone and try the next code.", "pwwrong": "The password is wrong.",
              "pwlimit": "Too many wrong attempts. Wait a few minutes."}


def trusted_networks(spec: str) -> list:
    """LAMINATOR_TRUSTED_PROXIES: the reverse proxies whose X-Forwarded-* headers are believed (IPs or CIDRs, comma separated).
    Nothing is trusted by default: a header from anyone else is ignored, so it cannot be used to fake an address."""
    nets = []
    for part in (spec or "").split(","):
        part = part.strip()
        if part:
            nets.append(ipaddress.ip_network(part, strict=False))  # a typo stops the start with a clear error
    return nets


def build_runtime(cfg: dict, data: Path):
    """({kind: connected server client}, MDBList client or None). Missing keys or addresses are not an error here:
    the UI starts anyway and sends you to Settings."""
    out = {}
    for kind in enabled_servers(cfg):
        try:
            out[kind] = connect(load_server(cfg, kind))
        except ConfigError:
            pass
    key = os.environ.get(MDBLIST_ENV)
    return out, (MDBList(key, data / "cache/mdblist") if key else None)


def create_app(cfg: dict, server, data: Path, password: str | None, mdb=None, sched: schedmod.Schedule | None = None,
               run_scheduler: bool = False, rebuild=None, reload_cfg=None, config_path: Path | None = None, setup: bool = False) -> FastAPI:
    """server: {kind: client} (or one client, taken to be the first configured kind); data: the shared data folder;
    password: None only for the explicit insecure local mode (then the UI is read-only)."""
    app = FastAPI(title="laminator", docs_url=None, redoc_url=None, openapi_url=None)
    if password is not None and not setup and (data / bk.BACKUP_DIR).is_dir():  # read the backups' summaries once in the background, so the first Settings visit is not the slow one
        threading.Thread(target=lambda: bk.list_backups(data), name="backup-index", daemon=True).start()
    tpls = Jinja2Templates(directory=str(HERE / "templates"))  # autoescape on: server data is untrusted
    build = re.sub(r"[^0-9a-zA-Z]", "", os.environ.get("LAMINATOR_BUILD", ""))[:12]
    tpls.env.globals["app_version"] = __version__ + (f"-dev+{build}" if build else "")   # a test image from the dev repository shows its commit
    tpls.env.filters["show"] = metamod.show_value   # a value as one readable line (lists joined, empty as (none))
    tpls.env.filters["tname"] = lambda s: str(s).replace("-", " ").replace("_", " ").title()   # default -> Default, my-look -> My Look
    tpls.env.globals["asset_v"] = str(int(max(f.stat().st_mtime for f in (HERE / "static").glob("*.*"))))  # new CSS/JS is fetched after an update
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    sec = SimpleNamespace(v=auth.load_secret(data))  # the key for CSRF tokens and the two-factor step (rotated with the password)
    store = sessmod.SessionStore(data)
    watch = storagemod.Watch(data).start()   # reads the data folder every 15 seconds (read-only); a probe that hangs means the disk is not responding
    weakref.finalize(app, watch.stop)
    app.state.storage = watch
    trusted = trusted_networks(os.environ.get("LAMINATOR_TRUSTED_PROXIES", ""))
    public = urlparse(os.environ.get("LAMINATOR_PUBLIC_URL", "").strip())
    mfa_tries: dict[str, int] = {}

    def via_proxy(request: Request) -> bool:
        peer = request.client.host if request.client else ""
        try:
            return any(ipaddress.ip_address(peer) in n for n in trusted)
        except ValueError:
            return False

    def client_ip(request: Request) -> str:
        """The real client address: from X-Forwarded-For only when the request came through a trusted proxy (the right-most address
        that is not itself a trusted proxy), else the connection's own address."""
        peer = request.client.host if request.client else "?"
        if not via_proxy(request):
            return peer
        for hop in reversed([h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]):
            try:
                if not any(ipaddress.ip_address(hop) in n for n in trusted):
                    return hop
            except ValueError:
                break
        return peer

    def is_https(request: Request) -> bool:
        if request.url.scheme == "https":
            return True
        return via_proxy(request) and request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"

    def allowed_hosts(request: Request) -> set[str]:
        hosts = {request.headers.get("host", "")}
        if via_proxy(request) and request.headers.get("x-forwarded-host"):
            hosts.add(request.headers["x-forwarded-host"].split(",")[0].strip())
        if public.netloc:
            hosts.add(public.netloc)
        return hosts

    def set_session(resp, request: Request, token: str):
        resp.set_cookie(auth.COOKIE, token, max_age=sessmod.ABSOLUTE, httponly=True, samesite="strict", secure=is_https(request))
        return resp

    def as_map(x) -> dict:
        if not x:
            return {}
        if isinstance(x, dict):
            return dict(x)
        return {(enabled_servers(cfg) or ["emby"])[0]: x}  # a single client, as older callers pass

    rt = SimpleNamespace(servers=as_map(server), mdb=mdb)  # swapped when a key or address changes (see settings)
    base_cfg = copy.deepcopy(cfg)
    limiter = auth.RateLimit()
    cache: dict = {}
    cands = pst.CandidateCache()
    overrides_file = data / "overrides.yaml"
    # without any password the UI stays read-only (a web page could otherwise POST to localhost)
    can_write = password is not None or passwords.load(data) is not None
    # first start: no password anywhere. The only thing the UI offers is choosing one, and only with a one-time code printed in the log,
    # so whoever merely reaches the page cannot claim the app. The password is then kept as a salted hash (admin.hash).
    setup_open = bool(setup and not can_write)
    setup_code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(8)) if setup_open else ""
    app.state.setup_code = f"{setup_code[:4]}-{setup_code[4:]}" if setup_open else ""
    jm = JobManager(data, on_done=lambda: cache.clear())
    app.state.jobs = jm
    tpls.env.globals["can_write"] = can_write
    tpls.env.filters["when"] = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))

    def csrf_token(request: Request) -> str:
        return hmac.new(sec.v, ("csrf:" + request.cookies.get(auth.COOKIE, "")).encode(), hashlib.sha256).hexdigest()

    tpls.env.globals["csrf"] = csrf_token

    # ---- which server --------------------------------------------------------------------------------
    def pick(request: Request, explicit: str | None = None) -> str | None:
        for cand in (explicit, request.query_params.get("s"), request.cookies.get(COOKIE_SRV)):
            if cand in rt.servers:
                return cand
        return next(iter(rt.servers), None)

    def sdir(kind: str) -> Path:
        return server_dir(cfg, kind, data)

    def store_for(kind: str) -> Store:
        return Store(sdir(kind) / "state.db")

    def page(request: Request, name: str, ctx: dict | None = None, status_code: int = 200, kind: str | None = None):
        k = kind or pick(request)
        nav = [(s, LABEL.get(s, s), s == k) for s in rt.servers] if len(rt.servers) > 1 else []
        return tpls.TemplateResponse(request, name, {**(ctx or {}), "srv": k or "", "srv_label": LABEL.get(k, ""), "nav_servers": nav,
                                                          "mdb_missing": rt.mdb is None and name != "settings.html"},
                                     status_code=status_code)

    def make_ws(kind: str, library: str | None = None) -> actions.Workspace:
        """A workspace for a job. Created inside the job thread (SQLite connections are per thread)."""
        s = servers(cfg).get(kind, {})
        scfg = ServerConfig(kind=kind, url=s.get("url", ""), api_key="", library=library)
        return actions.Workspace(cfg, scfg, rt.servers[kind], store_for(kind), rt.mdb, sdir(kind), data)

    def libs(kind: str) -> dict:
        """The server's libraries and items, cached for 30 s. An unreachable server gives {} plus a readable reason
        (see lib_error) instead of a crashed page."""
        c = cache.setdefault(kind, {"t": 0, "libs": {}, "err": None})
        if time.time() - c["t"] > 30:
            c["err"] = None
            try:
                c["libs"] = actions.all_libraries(rt.servers[kind]) if kind in rt.servers else {}
            except httpx.HTTPStatusError as e:
                c["libs"], c["err"] = {}, f"the server answered HTTP {e.response.status_code} (is the API key right?)"
            except httpx.HTTPError as e:
                c["libs"], c["err"] = {}, f"could not reach the server ({type(e).__name__}): check its address"
            c["t"] = time.time()
        return c["libs"]

    def lib_error(kind: str) -> str | None:
        return cache.get(kind, {}).get("err")

    def find(kind: str, item_id: str):
        for lib, items in libs(kind).items():
            for it in items:
                if it.id == item_id:
                    return lib, it
        return None, None

    def score_overrides(kind: str) -> dict:
        try:
            entries = metamod.for_server(metamod.load_overrides(overrides_file, required=False), kind)
            return metamod.score_overrides(entries, libs(kind))[0]
        except metamod.OverrideError:
            return {}

    def state_of(row, it) -> tuple[str, str]:
        if row is None:
            return "plain", "not managed"
        if row["applied_hash"] is None:
            return "orig", "original poster"
        if it.poster_tag != row["served_tag"]:
            return "warn", "changed on server"
        return "ok", f"overlay {row['score_text']}"

    @app.middleware("http")
    async def gate(request: Request, call_next):
        if watch.stalled and not request.url.path.startswith(("/healthz", "/static")):
            # the data folder is not answering (a stuck disk): say so at once instead of hanging on it
            return HTMLResponse(NOT_RESPONDING.format(secs=watch.seconds), status_code=503, headers={"Retry-After": "15", "Cache-Control": "no-store", **SECURITY_HEADERS})
        if setup_open and not request.url.path.startswith(("/setup", "/healthz", "/static")):
            return RedirectResponse("/setup", status_code=303)
        if can_write and not request.url.path.startswith(PUBLIC):
            if not store.check(request.cookies.get(auth.COOKIE)):
                resp = RedirectResponse("/login", status_code=303)
                for k, v in SECURITY_HEADERS.items():
                    resp.headers.setdefault(k, v)
                return resp
        if not rt.servers and request.url.path.startswith(NEEDS_SERVER):
            if request.url.path.startswith("/img/"):
                return Response("no media server connection", status_code=503)
            return RedirectResponse("/settings?note=noserver", status_code=303)
        resp = await call_next(request)
        for k, v in SECURITY_HEADERS.items():
            resp.headers.setdefault(k, v)
        if request.url.path.startswith(("/login", "/settings")):
            resp.headers.setdefault("Cache-Control", "no-store")  # nothing about sign-in or keys stays in a shared cache
        return resp

    # ---- auth --------------------------------------------------------------------------------
    def login_ok(given: str) -> bool:
        """The password saved from Settings (a hash) wins over LAMINATOR_ADMIN_PASSWORD, like saved API keys do."""
        stored = passwords.load(data)
        if stored:
            return passwords.verify(given, stored)
        return password is not None and auth.check_password(given, password)

    @app.get("/healthz")
    def healthz():
        if watch.stalled:  # a failing health check is how Docker (and you) find out the disk is stuck
            return JSONResponse({"ok": False, "storage": "not responding", "seconds": watch.seconds}, status_code=503)
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        return tpls.TemplateResponse(request, "login.html", {"error": None})

    def start_session(request: Request, resp, how: str):
        token = store.create(client_ip(request), request.headers.get("user-agent", ""))
        audit.log(data, "signed in", ip=client_ip(request), how=how)
        resp.delete_cookie(MFA_COOKIE)
        return set_session(resp, request, token)

    def mfa_pending_token(now: float) -> str:
        nonce = secrets.token_urlsafe(16)
        exp = int(now) + MFA_TTL
        mac = hmac.new(sec.v, f"mfa:{exp}:{nonce}".encode(), hashlib.sha256).hexdigest()
        return f"{exp}.{nonce}.{mac}"

    def mfa_pending_ok(token: str | None, now: float) -> str | None:
        """The nonce of a valid, unexpired password-passed step, or None."""
        try:
            exp, nonce, mac = (token or "").split(".")
            good = hmac.new(sec.v, f"mfa:{exp}:{nonce}".encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(mac, good) and int(exp) > now and mfa_tries.get(nonce, 0) < MFA_TRIES:
                return nonce
        except ValueError:
            pass
        return None

    def setup_page(request: Request, error: str | None = None, status_code: int = 200):
        return tpls.TemplateResponse(request, "setup.html", {"error": error, "pw_min": passwords.MIN_LEN}, status_code=status_code)

    @app.get("/setup", response_class=HTMLResponse)
    def setup_get(request: Request):
        if not setup_open:
            return RedirectResponse("/login" if can_write else "/library", status_code=303)
        return setup_page(request)

    @app.post("/setup")
    def setup_post(request: Request, code: str = Form(""), password_in: str = Form("", alias="password"), confirm: str = Form("")):
        nonlocal can_write, setup_open
        if not setup_open:
            return RedirectResponse("/login" if can_write else "/library", status_code=303)
        who = client_ip(request)
        if limiter.blocked(who):
            return setup_page(request, "Too many attempts. Wait a few minutes.", 429)
        given = "".join(ch for ch in code.upper() if ch.isalnum())
        if not hmac.compare_digest(given, setup_code):
            limiter.fail(who)
            audit.log(data, "wrong setup code", ip=who)
            return setup_page(request, "That setup code is not right. It is printed in the container's log (docker logs laminator).", 401)
        if password_in != confirm:
            return setup_page(request, "The two passwords do not match.", 422)
        err = passwords.policy_error(password_in)
        if err:
            return setup_page(request, f"Password: {err}.", 422)
        limiter.ok(who)
        passwords.save(data, password_in)
        audit.log(data, "password chosen (first start)", ip=who)
        can_write, setup_open = True, False  # the app is now an ordinary password-protected one
        tpls.env.globals["can_write"] = True
        start_scheduler()
        return start_session(request, RedirectResponse("/settings?note=setupdone", status_code=303), "first-start password")

    @app.post("/login")
    def login(request: Request, password_in: str = Form(alias="password")):
        who = client_ip(request)
        if limiter.blocked(who):
            audit.log(data, "sign-in blocked", ip=who)
            return tpls.TemplateResponse(request, "login.html", {"error": "Too many attempts. Wait a few minutes."}, status_code=429)
        if not login_ok(password_in):
            limiter.fail(who)
            audit.log(data, "wrong password", ip=who)
            return tpls.TemplateResponse(request, "login.html", {"error": "Wrong password."}, status_code=401)
        if mfa.enabled(data):  # the password was right: now the second step
            resp = RedirectResponse("/login/code", status_code=303)
            resp.set_cookie(MFA_COOKIE, mfa_pending_token(time.time()), max_age=MFA_TTL, httponly=True, samesite="strict", secure=is_https(request))
            return resp
        limiter.ok(who)
        return start_session(request, RedirectResponse("/library", status_code=303), "password")

    @app.get("/login/code", response_class=HTMLResponse)
    def login_code_page(request: Request):
        if not mfa_pending_ok(request.cookies.get(MFA_COOKIE), time.time()):
            return RedirectResponse("/login", status_code=303)
        return tpls.TemplateResponse(request, "login_code.html", {"error": None})

    @app.post("/login/code")
    def login_code(request: Request, code: str = Form("")):
        who, now = client_ip(request), time.time()
        nonce = mfa_pending_ok(request.cookies.get(MFA_COOKIE), now)
        if nonce is None:
            return RedirectResponse("/login", status_code=303)
        if limiter.blocked(who):
            return tpls.TemplateResponse(request, "login_code.html", {"error": "Too many attempts. Wait a few minutes."}, status_code=429)
        how = mfa.verify(data, code[:64], now)
        if how is None:
            limiter.fail(who)
            mfa_tries[nonce] = mfa_tries.get(nonce, 0) + 1
            if len(mfa_tries) > 1000:
                mfa_tries.clear()
            audit.log(data, "wrong two-factor code", ip=who)
            left = MFA_TRIES - mfa_tries[nonce]
            if left <= 0:
                resp = RedirectResponse("/login", status_code=303)
                resp.delete_cookie(MFA_COOKIE)
                return resp
            return tpls.TemplateResponse(request, "login_code.html", {"error": f"That code is not right. {left} more tries, then sign in again."}, status_code=401)
        limiter.ok(who)
        mfa_tries.pop(nonce, None)
        if how == "recovery":
            audit.log(data, "recovery code used", ip=who, left=mfa.recovery_left(data))
        return start_session(request, RedirectResponse("/library" if how == "code" else "/settings?note=mfarecovery", status_code=303), "password + " + how)

    @app.post("/logout")
    def logout(request: Request, token: str = Form("")):
        if can_write and not hmac.compare_digest(token, csrf_token(request)):
            return HTMLResponse("Bad or missing form token. Reload the page and try again.", status_code=403)
        store.delete(request.cookies.get(auth.COOKIE))
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(auth.COOKIE)
        return resp

    # ---- pages --------------------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/library", status_code=303)

    @app.get("/switch")
    def switch(s: str = "", next: str = "/library"):
        """Choose which server the pages show (a view preference, no write). `next` must be a local path."""
        dest = next if next.startswith("/") and not next.startswith("//") and "\\" not in next else "/library"
        resp = RedirectResponse(dest, status_code=303)
        if s in rt.servers:
            resp.set_cookie(COOKIE_SRV, s, max_age=365 * 86400, httponly=True, samesite="strict")
        return resp

    def override_ids(kind: str, only_scores: bool = False) -> set[str]:
        """Ids of the items an overrides.yaml entry matches (only those with a poster score when only_scores)."""
        try:
            entries = metamod.for_server(metamod.load_overrides(overrides_file, required=False), kind)
        except metamod.OverrideError:
            return set()
        found, by_lib = set(), libs(kind)
        for e in entries:
            if only_scores and "score" not in e:
                continue
            try:
                found.update(it.id for it in metamod.resolve(e, by_lib))
            except metamod.OverrideError:
                pass
        return found

    def has_mdblist_score(it) -> bool:
        """True when an earlier lookup saved a real combined score for the title (a 0 means none). Never asks MDBList."""
        mk = "movie" if it.type == "Movie" else "show"
        for prov in ("imdb", "tmdb", "tvdb"):
            pid = it.provider_ids.get(prov)
            d = mdbmod.cached_title(data / "cache" / "mdblist", prov, mk, pid) if pid else None
            if d and mdbmod.pick_score(d, ["aggregate"]) is not None:
                return True
        return False

    def match_show(kind: str, show: str, items: list) -> list:
        store = store_for(kind)
        if show in ("no_mdb", "has_mdb"):
            want = show == "has_mdb"
            return [it for it in items if has_mdblist_score(it) == want]
        if show == "my_score":
            ids = override_ids(kind, only_scores=True)
            return [it for it in items if it.id in ids]
        if show == "in_file":
            ids = override_ids(kind)
            return [it for it in items if it.id in ids]
        if show == "custom":
            idx = bp.AssetIndex(assets_root())
            return [it for it in items if idx.find(it) is not None]
        if show == "no_poster":
            return [it for it in items if not it.has_poster]
        if show == "thumb":  # the server's image is wider than tall (a video frame), not a poster
            return [it for it in items if (getattr(it, "aspect", None) or 0) > 0.9]
        if show == "zero":  # a badge drawn for a score of 0 (an earlier version did this for titles with no real score)
            return [it for it in items if (row := store.get(it.id)) is not None and row["applied_hash"] is not None and (row["score_text"] or "") in actions.ZERO_BADGES]
        if show == "meta_written":
            return [it for it in items if any(k != "critic_rating" for k in store.meta_get(it.id))]
        want = {"not_managed": "plain", "overlay": "ok", "original": "orig", "changed": "warn"}[show]
        return [it for it in items if state_of(store.get(it.id), it)[0] == want]

    def score_pct_of(kind: str, it, src: str, manual: dict):
        """The score (0-100) a title would carry for this source: a manual override first (combined score only), else what an
        earlier MDBList lookup saved. None when there is none. Never asks MDBList."""
        if src == "aggregate":
            v = manual.get(it.id)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return metamod.override_pct(v)
        mk = "movie" if it.type == "Movie" else "show"
        for prov in ("imdb", "tmdb", "tvdb"):
            pid = it.provider_ids.get(prov)
            d = mdbmod.cached_title(data / "cache" / "mdblist", prov, mk, pid) if pid else None
            if d:
                p = mdbmod.pick_score(d, [src])
                if p:
                    return p[2]
        return None

    def match_ranges(kind: str, items: list, f: dict) -> list:
        out = items
        if f["ymin"] is not None or f["ymax"] is not None:
            out = [it for it in out if it.year and (f["ymin"] is None or it.year >= f["ymin"]) and (f["ymax"] is None or it.year <= f["ymax"])]
        if f["crate"]:
            want = None if f["crate"] == NO_RATING else f["crate"]
            out = [it for it in out if (getattr(it, "rating", None) or None) == want]
        if f["smin"] is not None or f["smax"] is not None:
            manual = score_overrides(kind) if f["ssrc"] == "aggregate" else {}
            lo = None if f["smin"] is None else round(f["smin"] * 10)
            hi = None if f["smax"] is None else round(f["smax"] * 10)
            kept = []
            for it in out:
                p = score_pct_of(kind, it, f["ssrc"], manual)
                if p is not None and (lo is None or p >= lo) and (hi is None or p <= hi):
                    kept.append(it)
            out = kept
        return out

    def number(v: str, lo: float, hi: float, cast=float):
        try:
            x = cast(v)
        except (TypeError, ValueError):
            return None
        return None if x != x else min(max(x, lo), hi)

    def parse_filters(show: str, smin: str, smax: str, ssrc: str, ymin: str, ymax: str, crate: str) -> tuple[str, dict]:
        f = {"smin": number(smin, 0, 10), "smax": number(smax, 0, 10), "ssrc": ssrc if ssrc in SOURCES else "aggregate",
             "ymin": number(ymin, 1870, 2100, int), "ymax": number(ymax, 1870, 2100, int), "crate": crate[:24]}
        return (show if show in SHOW_LABEL else ""), f

    def describe_filters(show: str, f: dict) -> list[str]:
        out = [SHOW_LABEL[show]] if show else []
        if f["smin"] is not None or f["smax"] is not None:
            lo, hi = f["smin"], f["smax"]
            span = f"{lo:g} to {hi:g}" if lo is not None and hi is not None else f"at least {lo:g}" if lo is not None else f"at most {hi:g}"
            out.append(f"score {span} ({SOURCES[f['ssrc']]})")
        if f["ymin"] is not None or f["ymax"] is not None:
            lo, hi = f["ymin"], f["ymax"]
            out.append("year " + (f"{lo:g} to {hi:g}" if lo is not None and hi is not None else f"from {lo:g}" if lo is not None else f"up to {hi:g}"))
        if f["crate"]:
            out.append("content rating " + ("not set" if f["crate"] == NO_RATING else f["crate"]))
        return out

    def filter_query(kind, lib, q, view, per, show, f) -> str:
        fq = {"show": show, **{k: ("" if v is None else v) for k, v in f.items() if k != "ssrc" or f["smin"] is not None or f["smax"] is not None}}
        return urlencode({"s": kind or "", "lib": lib or "", "q": q, "view": view, "per": per, **{k: (f"{v:g}" if isinstance(v, float) else v) for k, v in fq.items()}})

    views_memo: dict = {}

    def listing(kind: str, lib, q: str, show: str, f: dict) -> tuple[list, dict]:
        """The titles to show, sorted, with where each letter starts. Remembered while the server's list is (30 s) and the file is
        unchanged, so a scroll batch does not redo the filtering."""
        key = (kind, lib, q.lower(), show, tuple(sorted(f.items())), cache.get(kind, {}).get("t"), overrides_file.stat().st_mtime if overrides_file.exists() else 0)
        if key in views_memo:
            return views_memo[key]
        items = [it for it in libs(kind).get(lib, []) if q.lower() in it.name.lower()]
        if show:
            items = match_show(kind, show, items)
        items = match_ranges(kind, items, f)
        items.sort(key=lambda i: sort_key(i.name))
        first_idx: dict = {}
        for n, it in enumerate(items):
            first_idx.setdefault(bucket(it.name), n)
        if len(views_memo) > 8:
            views_memo.clear()
        views_memo[key] = (items, first_idx)
        return items, first_idx

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, lib: str | None = None, q: str = "", page_no: int = 1, view: str = "", per: str = "", more: int = 0, show: str = "",
                smin: str = "", smax: str = "", ssrc: str = "aggregate", ymin: str = "", ymax: str = "", crate: str = ""):
        kind = pick(request)
        all_libs = libs(kind)
        names = list(all_libs)
        lib = lib if lib in all_libs else (names[0] if names else None)
        store = store_for(kind)
        chosen = view if view in VIEWS else request.cookies.get(COOKIE_VIEW, "")
        view_now = chosen if chosen in VIEWS else "posters"
        valid_per = {k for k, _ in PER_CHOICES}
        per_now = per if per in valid_per else request.cookies.get(COOKIE_PER, "")
        per_now = per_now if per_now in valid_per else "scroll"
        scroll = per_now == "scroll"
        size = VIEWS[view_now][1] if scroll else int(per_now)  # in scroll mode: how many titles each load adds
        show, f = parse_filters(show, smin, smax, ssrc, ymin, ymax, crate)
        items, first_idx = listing(kind, lib, q, show, f)  # first_idx: letter -> index of the first title with it (the A-Z rail target)
        pages = max(1, -(-len(items) // size))
        page_no = min(max(page_no, 1), pages)
        first_on_page = {L: n // size + 1 for L, n in first_idx.items()}
        # scroll mode: the page asked for is the last chunk to show; a "more" request adds just that chunk
        start = (page_no - 1) * size if (not scroll or more) else 0
        end = page_no * size
        cards = []
        for n, it in enumerate(items[start:end], start):
            cls, label = state_of(store.get(it.id), it)
            keys = sorted(store.meta_get(it.id))
            meta, meta_title = "", ""
            other = [k for k in keys if k != "critic_rating"]  # the CriticRating write is housekeeping: no label for it
            if other:
                meta = "metadata"
                meta_title = "Values from overrides.yaml (" + ", ".join(k.replace("_", " ") for k in other) + ") were written to this item in the server and locked."
            b_ = bucket(it.name)
            cards.append({"it": it, "cls": cls, "label": label, "meta": meta, "meta_title": meta_title, "state_title": STATE_TITLES.get(cls, ""), "anchor": b_ if first_idx[b_] == n else ""})
        keep = {"s": kind or "", "lib": lib or "", "q": q, "view": view_now, "per": per_now}
        reset = urlencode({k: v for k, v in keep.items() if k != "q"})  # everything back to the defaults; the library and the view stay
        base = filter_query(kind, lib, q, view_now, per_now, show, f)
        clear = urlencode(keep)
        shown_f = describe_filters(show, f)
        rating_counts: dict = {}
        for it in all_libs.get(lib, []):
            r = getattr(it, "rating", None) or NO_RATING
            rating_counts[r] = rating_counts.get(r, 0) + 1
        rating_options = [(r, "Not set" if r == NO_RATING else r, n) for r, n in sorted(rating_counts.items(), key=lambda kv: (kv[0] == NO_RATING, kv[0]))]
        rail = [{"l": L, "page": first_on_page.get(L)} for L in RAIL] if len(items) > size // 2 and len(first_on_page) > 1 else []
        sched_on = bool(sch.sched and (sch.sched.changed_every_minutes or sch.sched.maintenance_day))
        first_run = bool(kind) and not state_counts(sdir(kind))[0] and not sched_on  # nothing applied yet, nothing scheduled
        ctx = {"show": show, "show_groups": SHOW_GROUPS, "show_label": SHOW_LABEL.get(show, ""), "clear": clear, "reset": reset, "has_filters": bool(shown_f or q.strip()), "filters": shown_f, "f": f, "selectable": bool(shown_f or q.strip()) and bool(items) and can_write,
               "rating_options": rating_options, "score_sources": [(i, SOURCES[i]) for i in SOURCE_ORDER],
               "more_open": bool(shown_f and (show == "" or len(shown_f) > 1)), "first_run": first_run, "libs": names, "lib": lib, "q": q, "page_no": page_no, "pages": pages, "total": len(items), "cards": cards,
               "error": lib_error(kind), "view": view_now, "views": [(k, v[0]) for k, v in VIEWS.items()], "base": base, "rail": rail,
               "per": per_now, "pers": PER_CHOICES, "scroll": scroll, "has_more": scroll and end < len(items)}
        htmx = request.headers.get("HX-Request")
        resp = page(request, ("_more.html" if more and scroll else "_grid.html") if htmx else "library.html", ctx, kind=kind)
        if per in valid_per:  # remembered between visits (a display preference, nothing sensitive)
            resp.set_cookie(COOKIE_PER, per, max_age=365 * 86400, httponly=True, samesite="strict")
        if view in VIEWS:  # remember the choice (a display preference, nothing sensitive)
            resp.set_cookie(COOKIE_VIEW, view, max_age=365 * 86400, httponly=True, samesite="strict")
        return resp

    @app.get("/item/{item_id}", response_class=HTMLResponse)
    def item_page(request: Request, item_id: str, template: str = "default"):
        kind = pick(request)
        lib, it = find(kind, item_id)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        store = store_for(kind)
        row = store.get(it.id)
        cls, label = state_of(row, it)
        full = rt.servers[kind].get_full(it.id)
        fields = {k: full.get(k) for k in ("OfficialRating", "CriticRating", "CommunityRating", "OriginalTitle", "ForcedSortName")}
        score = None
        override = score_overrides(kind).get(it.id)
        if override is not None:
            score = ("override", metamod.format_score(override))
        elif rt.mdb is not None:
            try:
                tpl, _ = assets.load_template(template, data)
                pick_ = actions.choose_score(it, rt.mdb, tpl, {})
                score = (pick_[0], pick_[1]) if pick_ else None
            except Exception:  # MDBList unreachable: the page should still load
                score = None
        meta_rows = [{"key": k, "old": metamod.show_value(json.loads(r["old_value"])), "now": metamod.show_value(json.loads(r["override_value"]))}
                     for k, r in store.meta_get(it.id).items()]
        try:
            ratings = rt.servers[kind].rating_names(it.id) if can_write else []
        except Exception:  # the item page must still load if the server cannot list ratings
            ratings = []
        ids = [k for k in ("imdb", "tmdb", "tvdb") if it.provider_ids.get(k)]
        ov = existing_override(kind, lib, it)
        ctx = {"ov": ov, "ratings": ratings, "match_label": f"{ids[0].upper()} id" if ids else "library, title and year", "it": it, "lib": lib, "cls": cls, "label": label, "row": row, "fields": fields, "score": score,
               "meta_rows": meta_rows, "locked": full.get("LockedFields") or [], "template": template,
               "templates": template_names(), "custom": custom_poster_ctx(kind, it),
               "now": {k: metamod.show_value(metamod.get_value(full, k)) for k in ("title", "tagline", "critic_rating", "tags", "genres", "studios", "rating", "sort_title", "original_title")},
               "source_choices": [(i, SOURCES[i]) for i in SOURCE_ORDER], **usual_choices()}
        return page(request, "item.html", ctx, kind=kind)

    def existing_override(kind: str, lib: str, it) -> dict | None:
        """The overrides.yaml entry that already applies to this title, as form values, so the item page edits it instead of starting
        an empty second one. None when there is no entry (or the file cannot be read: the editor reports that)."""
        try:
            entries = metamod.parse_overrides(overrides_text())
        except metamod.OverrideError:
            return None
        i = metamod.matching_entry(entries, it, lib, kind)
        if i is None:
            return None
        e = entries[i]
        out: dict = {"index": i, "n": len(entries), "form": {}, "modes": {}}
        for k in ("rating", "title", "sort_title", "original_title", "overview", "tagline", "critic_rating", "score", "template", "source"):
            if k in e:
                out["form"][k] = e[k] if e[k] != "" else ""
        if isinstance(e.get("score"), (int, float)) and not isinstance(e["score"], bool):  # the file's 0-10 score, shown on the 0-100 scale
            out["form"]["score"] = format(Decimal(str(e["score"])) * 10, "f").rstrip("0").rstrip(".") or "0"
        for k in ("tagline", "overview"):
            out["modes"][k + "_clear"] = k in e and e[k] == ""
        for k in ("tags", "genres", "studios"):
            if k not in e:
                continue
            v = e[k]
            if v == []:
                out["modes"][k] = "clear"
            elif isinstance(v, dict):
                mode = "add" if "add" in v else "remove"
                out["modes"][k] = mode
                out["form"][k] = ", ".join(map(str, v.get(mode) or []))
            else:
                out["modes"][k] = "replace"
                out["form"][k] = ", ".join(map(str, v))
        out["no_overlay"] = e.get("overlay") is False
        return out

    def usual_choices() -> dict:
        """What a title gets when it has no override of its own: the scheduled runs' template and that template's first score source.
        The first entry of each dropdown on the item form is named after these, so it says what really applies."""
        name = sch.sched.template if sch.sched else "default"
        try:
            tpl, _ = assets.load_template(name, data)
        except (FileNotFoundError, ValueError):
            name, (tpl, _) = "default", assets.load_template("default", data)
        src = (tpl.get("sources") or ["aggregate"])[0]
        return {"usual_template": name, "usual_source": src, "usual_source_label": SOURCES.get(src, src)}

    def custom_poster_ctx(kind: str, it) -> dict:
        idx = bp.AssetIndex(assets_root())
        p = idx.find(it)
        cls, text = asset_status(kind, it, p) if p else ("", "")
        return {"rel": idx.rel(p) if p else "", "cls": cls, "text": text, "folder": bp.folder_name(it) or "", "root": str(assets_root())}

    def template_names() -> list[str]:
        names = {p.stem for base in (data / "templates", assets.BUNDLED / "templates") if base.is_dir() for p in base.glob("*.yaml")}
        return sorted(names, key=lambda n: (n != "default", n))  # Default first

    def custom_template(name: str) -> Path | None:
        """The user's own template file (never a bundled one), by exact slug."""
        f = data / "templates" / f"{name}.yaml"
        return f if TEMPLATE_SLUG.match(name) and f.is_file() and not f.is_symlink() else None

    def tuned(params, tpl: dict) -> dict:
        """Apply the editor's query parameters (clamped) to a template. Nothing is saved."""
        s, g = tpl["score"], tpl["gradient"]
        src = params.get("source")
        if src in SOURCES:  # the chosen provider first; films it has no score for fall back to MDBList's combined score
            tpl["sources"] = [src] if src == "aggregate" else [src, "aggregate"]

        def num(k):
            try:
                lo, hi = LIMITS[k]
                return min(max(float(params[k]), lo), hi) if k in params and params[k] != "" else None
            except ValueError:
                return None

        for key, (d, field) in {"gh": (g, "height"), "mo": (g, "max_opacity"), "sz": (s, "size"),
                                "mx": (s, "margin_x"), "my": (s, "margin_y"), "em": (s, "embolden")}.items():
            v = num(key)
            if v is not None:
                d[field] = v
        f = params.get("font")
        if f and f in assets.list_fonts(data):
            s["font"] = str(assets.font_path(f, data))
        return tpl

    def preview_jpeg(kind: str, it, tpl: dict, text: str) -> bytes:
        row = store_for(kind).get(it.id)
        orig = actions.original_file(row, sdir(kind) / "originals") if row else None
        raw = orig.read_bytes() if orig and orig.exists() else rt.servers[kind].download_primary(it.id, max_width=900)[0]
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((600, 900))  # templates are fractions of the poster, so a small copy looks the same and is fast
        buf = io.BytesIO(); img.save(buf, "JPEG", quality=90)
        return render(buf.getvalue(), text, tpl)

    @app.get("/templates", response_class=HTMLResponse)
    def templates_page(request: Request, name: str = "default", item: str | None = None):
        return render_templates(request, pick(request), name, item,
                                note=(request.query_params.get("nk", ""), TPL_NOTES.get(request.query_params.get("note", ""), "")))

    def render_templates(request: Request, kind, name: str, item: str | None, params=None, note=("", ""), status_code: int = 200):
        """The Templates page. `params` (a refused save) puts back exactly the values that were on screen."""
        items_all = [i for v in libs(kind).values() for i in v if i.has_poster]
        it = find(kind, item)[1] if item else (items_all[0] if items_all else None)
        tpl, _ = assets.load_template(name, data)
        if params is not None:
            tpl = tuned(params, tpl)
        ctx = {"names": template_names(), "name": name, "it": it, "fonts": assets.list_fonts(data),
               "t": tpl, "font_name": Path(tpl["score"]["font"]).name, "items": items_all[:200],
               "sources": [{"id": i, "label": SOURCES[i]} for i in SOURCE_ORDER], "source": (tpl.get("sources") or ["aggregate"])[0],
               "is_custom": custom_template(name) is not None, "note_kind": note[0], "note_text": note[1],
               "score_text": str(params.get("score", "")) if params is not None else "", "new_name": str(params.get("new_name", "")) if params is not None else ""}
        return page(request, "templates.html", ctx, kind=kind, status_code=status_code)

    def exists_message(slug: str) -> str:
        return (f"A template called {slug!r} already exists. To change it, pick it in the Template list and press Save with the Save as box empty, "
                "or enter a different name to make a new one.")

    @app.post("/templates/save")
    async def template_save(request: Request):
        form = await request.form()
        refusal = guard(request, str(form.get("token", "")))
        if refusal:
            return refusal
        base, new_name = str(form.get("name", "default")), str(form.get("new_name", "")).strip()
        kind = str(form.get("s", "")) if str(form.get("s", "")) in rt.servers else pick(request)
        item = str(form.get("item", "")) or None
        try:
            assets.load_template(base, data)
        except (FileNotFoundError, ValueError):
            return RedirectResponse("/templates?note=nodel&nk=bad", status_code=303)

        def refuse(message: str):
            return render_templates(request, kind, base, item, params=form, note=("bad", message), status_code=422)

        if new_name:  # a name means a new template
            slug = re.sub(r"[^a-z0-9]+", "-", new_name.lower()).strip("-")[:40]
            if not slug or not TEMPLATE_SLUG.match(slug):
                return refuse(TPL_NOTES["badname"])
            if slug in template_names():
                return refuse(exists_message(slug))
        else:  # no name: update the template that is open (the built-in Default is saved as your own copy of it)
            slug = base
            if not TEMPLATE_SLUG.match(slug):
                return refuse(TPL_NOTES["badname"])
        tpl, _ = assets.load_template(base, data)
        tpl = tuned({k: str(v) for k, v in form.items()}, tpl)
        tpl["score"]["font"] = Path(tpl["score"]["font"]).name  # stored by file name: it resolves in a container too
        out = data / "templates"; out.mkdir(exist_ok=True)
        tmp = out / f".{slug}.yaml.tmp"
        tmp.write_text("# saved from the Template page\n" + yaml.safe_dump(tpl, sort_keys=False, default_flow_style=None))
        tmp.replace(out / f"{slug}.yaml")
        q = urlencode({"name": slug, "note": "saved" if new_name else "updated", "nk": "ok", **({"s": kind} if kind else {})})
        return RedirectResponse(f"/templates?{q}", status_code=303)

    # ---- editing a template as YAML ------------------------------------------------------------------------------
    def yaml_page(request: Request, kind, name: str, text: str, item: str | None, errors=None, ok=False, preview=None, new_name="", status_code=200):
        ctx = {"name": name, "text": text, "errors": errors or [], "ok": ok, "preview": preview, "item": item or "", "new_name": new_name,
               "is_custom": custom_template(name) is not None, "fonts": assets.list_fonts(data)}
        return page(request, "template_yaml.html", ctx, kind=kind, status_code=status_code)

    def yaml_preview(kind, item: str | None, tpl: dict) -> str | None:
        """A data: URI of the template drawn on the chosen item's poster (or a plain sample), so nothing is stored."""
        import base64
        try:
            t2 = {**tpl, "score": {**tpl["score"], "font": str(assets.font_path(tpl["score"]["font"], data))}}
            it = find(kind, item)[1] if item and kind else None
            if it is not None:
                jpeg = preview_jpeg(kind, it, t2, "7.3")
            else:
                buf = io.BytesIO(); Image.new("RGB", (400, 600), (60, 90, 140)).save(buf, "JPEG")
                jpeg = render(buf.getvalue(), "7.3", t2)
            return "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()
        except Exception:
            return None

    @app.get("/templates/yaml", response_class=HTMLResponse)
    def template_yaml_page(request: Request):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        kind = pick(request)
        p = request.query_params
        name = p.get("name", "default")
        try:
            tpl, _ = assets.load_template(name, data)
        except (FileNotFoundError, ValueError):
            return HTMLResponse("Unknown template", status_code=404)
        tpl = tuned(p, tpl)  # start from what the sliders on the Templates page show right now
        tpl["score"]["font"] = Path(tpl["score"]["font"]).name
        text = yaml.safe_dump(tpl, sort_keys=False, default_flow_style=None)
        return yaml_page(request, kind, name, text, p.get("item"))

    @app.post("/templates/yaml")
    async def template_yaml_post(request: Request):
        form = await request.form()
        refusal = guard(request, str(form.get("token", "")))
        if refusal:
            return refusal
        kind = str(form.get("s", "")) if str(form.get("s", "")) in rt.servers else pick(request)
        text, name, item = str(form.get("text", "")), str(form.get("name", "default")), str(form.get("item", "")) or None
        new_name = str(form.get("new_name", "")).strip()
        tpl, errors = assets.validate_template_text(text, data)
        if errors:
            return yaml_page(request, kind, name, text, item, errors=errors, new_name=new_name, status_code=422)
        if form.get("action") != "save":
            return yaml_page(request, kind, name, text, item, ok=True, preview=yaml_preview(kind, item, tpl), new_name=new_name)
        if new_name:  # a name means a new template; an empty box updates the one being edited
            slug = re.sub(r"[^a-z0-9]+", "-", new_name.lower()).strip("-")[:40]
            if not slug or not TEMPLATE_SLUG.match(slug):
                return yaml_page(request, kind, name, text, item, errors=["Use letters and numbers in the name."], new_name=new_name, status_code=422)
            if slug in template_names():
                return yaml_page(request, kind, name, text, item, errors=[exists_message(slug)], new_name=new_name, status_code=422)
        else:
            slug = name
            if not TEMPLATE_SLUG.match(slug):
                return yaml_page(request, kind, name, text, item, errors=["Unknown template."], new_name=new_name, status_code=422)
        out = data / "templates"; out.mkdir(exist_ok=True)
        tmp = out / f".{slug}.yaml.tmp"
        tmp.write_text(text if text.endswith("\n") else text + "\n")  # exactly what was checked, comments and all
        tmp.replace(out / f"{slug}.yaml")
        q = urlencode({"name": slug, "note": "saved" if new_name else "updated", "nk": "ok", **({"s": kind} if kind else {})})
        return RedirectResponse(f"/templates?{q}", status_code=303)

    @app.post("/templates/delete")
    def template_delete(request: Request, name: str = Form(), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        f = custom_template(name)
        if f is None:
            return RedirectResponse("/templates?note=nodel&nk=bad", status_code=303)
        if name != "default" and name == (sch.sched.template if sch.sched else "default"):
            return RedirectResponse(f"/templates?name={name}&note=inuse&nk=bad", status_code=303)
        f.unlink()  # for Default this goes back to the built-in look
        return RedirectResponse(f"/templates?note={'reset' if name == 'default' else 'deleted'}&nk=ok", status_code=303)

    @app.get("/templates/panel", response_class=HTMLResponse)
    def template_panel(request: Request):
        p = dict(request.query_params)
        kind = pick(request)
        name, item = p.get("name", "default"), p.get("item")
        tpl, _ = assets.load_template(name, data)
        tpl = tuned(request.query_params, tpl)
        shown = {**tpl, "score": {**tpl["score"], "font": Path(tpl["score"]["font"]).name}}
        qs = urlencode([(k, v) for k, v in request.query_params.multi_items() if k in LIMITS or k in ("name", "item", "font", "score", "s", "source")])
        return page(request, "_panel.html", {
            "it": find(kind, item)[1] if item else None, "qs": qs,
            "yaml": yaml.safe_dump(shown, sort_keys=False, default_flow_style=None)}, kind=kind)

    # ---- running things ------------------------------------------------------------------------------------
    def apply_work(kind, library, item, template, only_changed, only_ids=None):
        ws = make_ws(kind, library)
        if ws.mdb is None:  # before anything is touched on the server
            raise actions.SetupError("The MDBList API key is not set, so there are no scores to draw. Add it under Settings, MDBList, press Save and test, then run this again.")
        tpl, sig = assets.load_template(template, data)
        extra = {"only_ids": only_ids} if only_ids is not None else {}  # only a chosen set of titles passes this
        yield from actions.apply_posters(ws, tpl, sig, overrides_file, False, item=item,
                                         critic=bool(cfg.get("critic_rating", False)), only_changed=only_changed, **extra)

    def maintenance_work(kind: str, template: str):
        """Full pass + cleanup, the same sequence as `laminator maintain`."""
        ws = make_ws(kind)
        if ws.mdb is None:
            raise actions.SetupError("The MDBList API key is not set, so there are no scores to draw. Add it under Settings, MDBList, press Save and test, then run this again.")
        tpl, sig = assets.load_template(template, data)
        yield from maintenance.run(ws, tpl, sig, overrides_file, False, bool(cfg.get("critic_rating", False)), True,
                                   cln.Limits.from_cfg(cfg))

    def scheduled_run(kind: str | None, what: str):
        """Start a scheduled job: 'changed' is the cheap check and 'maintenance' the full monthly pass (both per server);
        'backup' is a backup of laminator's own data (kind is None: it belongs to no one server)."""
        if what == "backup":
            def backup_work():
                keep = sch.sched.backup_keep if sch.sched else 5
                dest = dst.from_cfg(cfg)
                pending = schedmod.load(data).get("push_pending")
                newest = None
                if pending and dest:  # the last copy to the destination failed: copy the newest local scheduled backup again
                    autos = sorted(bk.backups_dir(data).glob(bk.AUTO_PREFIX + "*.tar.gz"), key=lambda f: f.name)
                    newest = autos[-1] if autos else None
                if newest is None:
                    yield ev("info", message="creating the scheduled backup...")
                    rep, pruned = bk.create_auto(data, keep, config_path)
                    newest = rep.path
                    schedmod.mark(data, "backup")
                    yield ev("backup", rep.path.name, f"{rep.size / 1e6:.1f} MB, {rep.files} files, verified; keys and password not included")
                    if pruned:
                        yield ev("info", message=f"  removed {len(pruned)} older scheduled backup(s) beyond the {keep} kept")
                if dest is None:
                    schedmod.update(data, push_pending=False, push_error="")
                    return
                try:
                    gone = dst.push(newest, dest, keep)
                    schedmod.update(data, push_pending=False, push_error="", last_push=time.time())
                    yield ev("backup", newest.name, f"copied to {dest.describe()} and checked there")
                    if gone:
                        yield ev("info", message=f"  removed {len(gone)} older scheduled backup(s) there beyond the {keep} kept")
                except dst.DestError as e:
                    schedmod.update(data, push_pending=True, push_error=str(e))
                    yield ev("warn", newest.name, f"the local backup is fine, but the copy to {dest.describe()} failed: {e} (retrying hourly)")
            return jm.start("Scheduled backup of laminator's data", backup_work, queue=False)
        if kind not in rt.servers:
            raise actions.Busy(f"{kind} is not connected (set its address and API key in Settings)")
        template = sch.sched.template if sch.sched else "default"

        def work():
            if what == "maintenance":
                yield from maintenance_work(kind, template)
            else:
                yield from apply_work(kind, None, None, template, True)

        label = LABEL.get(kind, kind)
        return jm.start(f"{label}: scheduled maintenance (full pass)" if what == "maintenance"
                        else f"{label}: scheduled check (new or changed posters)", work, queue=False, interruptible=True)

    sch = SimpleNamespace(sched=sched, obj=None)
    def start_scheduler() -> None:
        if sched and can_write and sch.obj is None:
            sch.obj = Scheduler(sched, lambda: {k: sdir(k) for k in rt.servers}, scheduled_run, root=data, paused=lambda: watch.stalled)
            app.state.scheduler = sch.obj
            if run_scheduler:
                sch.obj.start()

    app.state.scheduler = None
    start_scheduler()

    # ---- guards and shared helpers for the settings writes ----------------------------------------------------
    def guard(request: Request, token: str):
        """None if this write is allowed, else the refusal response."""
        if not can_write:
            return HTMLResponse("Settings are read-only without a password.", status_code=403)
        origin = request.headers.get("origin")
        if origin and urlparse(origin).netloc not in allowed_hosts(request):
            return HTMLResponse("Cross-site request refused (if this is your own reverse proxy, see Security in the user guide).", status_code=403)
        if not hmac.compare_digest(token, csrf_token(request)):
            return HTMLResponse("Bad or missing form token. Go back, reload the page and try again.", status_code=403)
        return None

    def reconnect() -> None:
        if rebuild:
            res = rebuild()
            rt.servers, rt.mdb = as_map(res[0]), res[1]
        cache.clear()

    def reload_config() -> None:
        """Re-read config + saved settings into the live cfg (in place: every closure shares this dict)."""
        new = reload_cfg() if reload_cfg else apply_settings(copy.deepcopy(base_cfg), data)
        cfg.clear()
        cfg.update(new)
        if sch.obj:
            try:
                sch.sched = sch.obj.sched = schedmod.parse(cfg)
            except ConfigError:
                pass

    def back(note: str, detail: str = ""):
        q = urlencode({"note": note, **({"detail": detail[:160]} if detail else {})})
        return RedirectResponse(f"/settings?{q}", status_code=303)

    def key_name_to_kind(name: str) -> str | None:
        return next((k for k in KINDS if server_key_env(cfg, k) == name), None)

    def allowed_names() -> set[str]:
        return {server_key_env(cfg, k) for k in KINDS} | {MDBLIST_ENV}

    def test_key(name: str) -> tuple[bool, str]:
        """Ask the service whether the key works. Never includes the key in the message."""
        try:
            if name == MDBLIST_ENV:
                if rt.mdb is None:
                    return False, "no MDBList key set"
                i = rt.mdb.check()
                return True, f"MDBList account OK: plan {i['plan']}, {i['remaining']} of {i['limit']} requests left today"
            kind = key_name_to_kind(name)
            if kind not in rt.servers:
                return False, f"no {LABEL.get(kind, 'server')} connection: its address or key is missing"
            return True, f"{LABEL[kind]}: connected to {rt.servers[kind].ping()}"
        except MDBListError as e:
            return False, str(e)
        except httpx.HTTPStatusError as e:
            return False, f"the service rejected the key (HTTP {e.response.status_code})"
        except httpx.HTTPError as e:
            return False, f"could not reach the service ({type(e).__name__})"

    def connection_state(kind: str) -> tuple[str, str]:
        """(css class, text): an honest check, not just 'a client object exists'."""
        if kind not in rt.servers:
            return "warn", "not connected (no usable key or address)"
        try:
            return "ok", f"connected to {rt.servers[kind].ping()}"
        except httpx.HTTPStatusError as e:
            return "bad", f"the server rejected the key (HTTP {e.response.status_code})"
        except httpx.HTTPError as e:
            return "bad", f"could not reach the server ({type(e).__name__})"

    def key_row(name: str, label: str) -> dict:
        src = secrets_store.source(data, name)
        return {"name": name, "label": label, "source": src, "masked": secrets_store.mask(os.environ.get(name)),
                "editable": can_write, "clearable": src == "settings" and can_write,
                "shadows": secrets_store.overrides_environment(data, name)}

    # ---- settings page ------------------------------------------------------------------------------------------
    def server_cards(form: dict | None = None) -> list[dict]:
        conf, saved = servers(cfg), load_settings(data).get("servers", {})
        cards = []
        for kind in KINDS:
            sv = conf.get(kind)
            enabled = bool(sv) and sv.get("enabled", True)
            posters, metas = state_counts(sdir(kind)) if sv else (0, 0)
            cards.append({"kind": kind, "label": LABEL[kind], "configured": bool(sv), "enabled": enabled,
                          "url": (sv or {}).get("url") or "", "library": (sv or {}).get("library") or "",
                          "key": key_row(server_key_env(cfg, kind), f"{LABEL[kind]} API key"),
                          "status": connection_state(kind) if enabled else None, "from_settings": kind in saved,
                          "managed": posters + metas, "libraries": sorted(libs(kind)) if kind in rt.servers else [],
                          "scheduled": schedmod.scheduled_on(sdir(kind)) if sv else False,
                          "warning": ("Checked against Jellyfin 10.11 with movies: posters, metadata and restore work. Jellyfin cannot lock a sort title, original title, "
                                      "tagline or critic rating, so a refresh there can undo those (the next apply writes them again). TV shows are less tested."
                                      if kind == "jellyfin" else "")})
            if form and form.get("kind") == kind:  # a save that was refused: show what was typed, not what is stored
                cards[-1].update({"url": form["url"], "library": form["library"], "enabled": form["enabled"], "configured": True})
        return cards

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, note: str = "", detail: str = ""):
        return render_settings(request, note, detail)

    def render_settings(request: Request, note: str = "", detail: str = "", form: dict | None = None, status_code: int = 200):
        kind_, text = NOTES.get(note, ("", ""))
        sc = schedmod.parse(cfg)
        ctx = {"cards": server_cards(form), "form_kind": (form or {}).get("kind", ""), "mdb_key": key_row(MDBLIST_ENV, "MDBList API key"),
               "note_kind": kind_, "note_text": text or detail, "note_detail": detail if text else "",
               "mfa_on": mfa.enabled(data), "mfa_left": mfa.recovery_left(data), "sessions": store.active(),
               "pw_saved": passwords.load(data) is not None, "pw_env": password is not None, "pw_min": passwords.MIN_LEN,
               "secrets_file": str(secrets_store.path(data).resolve()), "settings_file": str((data / "settings.yaml").resolve()),
               "critic": bool(cfg.get("critic_rating", False)), "sched": sc, "templates": template_names(),
               "general_saved": any(k in load_settings(data) for k in ("critic_rating", "schedule", "cleanup")),
               "cleanup_limits": cln.Limits.from_cfg(cfg),
               "dest": {**{"type": "none", "path": "", "host": "", "port": 445, "share": "", "folder": "", "username": "", "domain": ""},
                        **(cfg.get("backup_destination") if isinstance(cfg.get("backup_destination"), dict) else {})},
               "assets_value": cfg.get("assets_dir") if isinstance(cfg.get("assets_dir"), str) else "", "assets_default": str((data / "assets").resolve()),
               "dest_pw_set": bool(os.environ.get(dst.SMB_PASSWORD_ENV)),
               "dest_saved": "backup_destination" in load_settings(data),
               "dest_state": {k: v for k, v in schedmod.load(data).items() if k in ("push_error", "last_push")},
               "minutes_options": [0, 5, 15, 30, 60, 180, 360, 720, 1440],
               "backups": [{"name": r.path.name, "mb": round(r.size / 1e6, 1), "created": r.created, "has_secrets": r.has_secrets,
                            "servers": ", ".join(f"{LABEL.get(k, k)}: {v['posters']} poster{'' if v['posters'] == 1 else 's'}, {v['meta']} metadata" for k, v in r.servers.items()),
                            "problem": r.notes[0] if r.notes and not r.created else ""} for r in bk.list_backups(data)],
               "max_upload_mb": MAX_UPLOAD // (1024 * 1024)}
        return page(request, "settings.html", ctx, status_code=status_code)

    # ---- API keys ------------------------------------------------------------------------------------------------
    @app.post("/settings/key")
    def settings_save(request: Request, name: str = Form(), value: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if name not in allowed_names():
            return HTMLResponse("Unknown setting", status_code=400)
        if jm.running():
            return back("busy")
        try:
            secrets_store.save(data, name, value.strip())
        except ValueError:
            return back("bad", "That does not look like an API key (8-200 characters: letters, digits and . _ ~ + / = -).")
        reconnect()
        ok, msg = test_key(name)
        return back("ok" if ok else "fail", ("Saved. " if ok else "Saved, but: ") + msg)

    @app.post("/settings/clear")
    def settings_clear(request: Request, name: str = Form(), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if name not in allowed_names():
            return HTMLResponse("Unknown setting", status_code=400)
        if jm.running():
            return back("busy")
        if secrets_store.source(data, name) == "settings":
            secrets_store.clear(data, name)
            reconnect()
        return back("cleared")

    @app.post("/settings/test")
    def settings_test(request: Request, name: str = Form(), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if name not in allowed_names():
            return HTMLResponse("Unknown setting", status_code=400)
        ok, msg = test_key(name)
        return back("ok" if ok else "fail", msg)

    # ---- servers: address, library, on/off -----------------------------------------------------------------------
    @app.post("/settings/server")
    def settings_server(request: Request, kind: str = Form(), url: str = Form(""), library: str = Form(""),
                        enabled: str = Form(""), decision: str = Form(""), api_key: str = Form(""), token: str = Form(""),
                        scheduled_set: str = Form(""), scheduled: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if kind not in KINDS:
            return HTMLResponse("Unknown server kind", status_code=400)
        typed = {"kind": kind, "url": url.strip(), "library": library.strip(), "enabled": enabled == "1"}

        def refuse(message: str):  # show the page again with everything as typed, so nothing has to be entered twice
            return render_settings(request, "bad", message, form=typed, status_code=422)

        if jm.running():
            return back("busy")
        key_saved = False
        if api_key.strip():  # the one Save button stores the key too (first, so a mistake in the address does not lose it)
            try:
                secrets_store.save(data, server_key_env(cfg, kind), api_key.strip())
                key_saved = True
            except ValueError:
                return refuse("That does not look like an API key (8-200 characters: letters, digits and . _ ~ + / = -).")
        try:
            new_url = normalize_url(url) if url.strip() else ""
            lib = validate_library(library)
        except ValueError as e:
            return refuse(f"{LABEL[kind]}: {e}" + (" The API key was saved." if key_saved else ""))
        if enabled == "1" and not new_url:
            return refuse(f"{LABEL[kind]}: enter its address before switching it on." + (" The API key was saved." if key_saved else ""))
        typed["url"] = new_url
        old = servers(cfg).get(kind, {})
        old_url = normalize_url(old["url"]) if old.get("url") else ""
        posters, metas = state_counts(sdir(kind))
        if old_url and new_url != old_url and (posters + metas) and decision not in ("keep", "archive"):
            # a different address might be a different server: do not mix its items with the saved restore data
            return page(request, "server_confirm.html", {"kind": kind, "label": LABEL[kind], "old_url": old_url, "new_url": new_url,
                                                       "library": lib or "", "enabled": enabled, "n": posters + metas}, kind=kind)
        if old_url and new_url != old_url and decision == "archive":
            archive_state(sdir(kind))
        saved = load_settings(data)
        saved.setdefault("servers", {})[kind] = {"url": new_url, "library": lib, "enabled": enabled == "1"}
        save_settings(data, saved)
        if scheduled_set == "1":  # the box is on the form: record whether scheduled runs include this server
            sdir(kind).mkdir(parents=True, exist_ok=True)
            schedmod.update(sdir(kind), scheduled=scheduled == "1")
        reload_config()
        reconnect()
        if kind in rt.servers:
            ok, msg = test_key(server_key_env(cfg, kind))
            return back("ok" if ok else "fail", ("Saved. " if ok else "Saved, but: ") + msg)
        return back("srvsaved", f"{LABEL[kind]} saved. Add its API key to connect.")

    RESET_TEXT = {
        "general": ("Go back to the config file for the options?",
                    "The options you saved in this web page (the CriticRating switch, how often posters are checked, the scheduled template, the monthly maintenance, the clean-up and trash times and the backup schedule) are forgotten. laminator then uses what config.local.yaml says, or its built-in defaults where the file says nothing.",
                    "Nothing on your media server changes. API keys, saved servers and your posters are not touched.", "Yes, use the config file values"),
        "destination": ("Remove the second backup copy?",
                        "laminator stops copying backups to the folder or SMB share you set, and forgets the saved SMB password.",
                        "Backups already copied there stay where they are, and backups in laminator's own folder are not touched.", "Yes, remove it"),
    }

    @app.get("/settings/reset", response_class=HTMLResponse)
    def settings_reset_confirm(request: Request, section: str = ""):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        if section.startswith("server:") and section[7:] in KINDS:
            label = LABEL[section[7:]]
            text = (f"Go back to the config file for {label}?",
                    f"The {label} address, library and on/off setting you saved in this web page are forgotten. laminator then uses what config.local.yaml says for {label}, or treats it as not set up if the file has nothing.",
                    f"The {label} API key is not touched, and nothing on the media server changes.", "Yes, use the config file values")
        else:
            text = RESET_TEXT.get(section)
        if text is None:
            return HTMLResponse("Unknown section", status_code=400)
        title, what, keeps, yes = text
        return page(request, "reset_confirm.html", {"section": section, "title": title, "what": what, "keeps": keeps, "yes": yes})

    @app.post("/settings/reset")
    def settings_reset(request: Request, section: str = Form(), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        saved = load_settings(data)
        if section == "general":
            saved.pop("critic_rating", None)
            saved.pop("schedule", None)
            saved.pop("cleanup", None)
        elif section == "destination":
            saved.pop("backup_destination", None)
            secrets_store.clear(data, dst.SMB_PASSWORD_ENV)
        elif section.startswith("server:") and section[7:] in KINDS:
            saved.get("servers", {}).pop(section[7:], None)
            if not saved.get("servers"):
                saved.pop("servers", None)
        else:
            return HTMLResponse("Unknown section", status_code=400)
        if jm.running():
            return back("busy")
        save_settings(data, saved)
        reload_config()
        reconnect()
        return back("reset")

    # ---- options: CriticRating and the schedule ------------------------------------------------------------------
    @app.post("/settings/general")
    def settings_general(request: Request, critic: str = Form(""), changed_every_minutes: str = Form("30"),
                         maint_on: str = Form(""), maint_day: str = Form("1"), maint_time: str = Form("03:00"),
                         sched_template: str = Form("default"), grace_days: str = Form("30"), trash_days: str = Form("90"),
                         backup_every_days: str = Form(""), backup_keep: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        cur = schedmod.parse(cfg)  # the backup schedule has its own form (Backup and restore); blank here keeps it
        try:
            minutes = int(changed_every_minutes)
            candidate = {"changed_every_minutes": minutes, "template": sched_template,
                         "maintenance": {"day": int(maint_day), "time": maint_time} if maint_on == "1" else "off",
                         "backup": {"every_days": int(backup_every_days or cur.backup_every_days), "keep": int(backup_keep or cur.backup_keep)}}
            schedmod.parse({"schedule": candidate})  # the same validation the config file gets
            assets.template_file(sched_template, data)
            clean = {"grace_days": int(grace_days), "trash_days": int(trash_days)}
            cln.Limits.from_cfg({"cleanup": clean})
        except (ValueError, ConfigError, FileNotFoundError) as e:
            return back("bad", f"Options: {e}")
        saved = load_settings(data)
        saved["critic_rating"] = critic == "1"
        saved["schedule"] = candidate
        saved["cleanup"] = clean
        save_settings(data, saved)
        reload_config()
        return back("genok")

    @app.post("/settings/backup/schedule")
    def settings_backup_schedule(request: Request, backup_every_days: str = Form("7"), backup_keep: str = Form("5"), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        cur = schedmod.parse(cfg)
        try:
            candidate = {"changed_every_minutes": cur.changed_every_minutes, "template": cur.template,
                         "maintenance": {"day": cur.maintenance_day, "time": cur.maintenance_time} if cur.maintenance_day else "off",
                         "backup": {"every_days": int(backup_every_days), "keep": int(backup_keep)}}
            schedmod.parse({"schedule": candidate})
        except (ValueError, ConfigError) as e:
            return back("bad", f"Backup schedule: {e}")
        saved = load_settings(data)
        saved["schedule"] = candidate
        save_settings(data, saved)
        reload_config()
        return back("genok")

    # ---- custom base posters -----------------------------------------------------------------------------------
    def assets_root() -> Path:
        return bp.assets_root(cfg, data)

    def asset_status(kind: str, it, path: Path) -> tuple[str, str]:
        """(badge class, text) for an item's custom poster."""
        try:
            h = actions.sha(bp.load(path))
        except bp.AssetError as e:
            return "bad", f"unusable: {e}"
        row = store_for(kind).get(it.id)
        if row is not None and row["original_sha"] == h and row["applied_hash"] is not None:
            return "ok", "in use"
        return "warn", "waiting for the next run"

    @app.get("/assets", response_class=HTMLResponse)
    def assets_page(request: Request, note: str = "", detail: str = ""):
        kind = pick(request)
        root = assets_root()
        idx = bp.AssetIndex(root)
        matched, used = [], set()
        for lib, items in libs(kind).items():
            for it in items:
                p = idx.find(it)
                if p is not None:
                    used.add(p)
                    cls, text = asset_status(kind, it, p)
                    matched.append({"id": it.id, "name": it.name, "year": it.year, "lib": lib, "rel": idx.rel(p), "cls": cls, "text": text})
        matched.sort(key=lambda m: sort_key(m["name"]))
        unmatched = sorted(idx.rel(p) for p in idx.files.values() if p not in used)
        nk, nt = ASSET_NOTES.get(note, ("", ""))
        ctx = {"root": str(root), "exists": root.is_dir(), "matched": matched, "unmatched": unmatched[:300], "n_unmatched": len(unmatched),
               "default_root": str((data / "assets").resolve()), "custom_root": isinstance(cfg.get("assets_dir"), str), "waiting": sum(1 for m in matched if m["cls"] != "ok"),
               "note_kind": nk, "note_text": nt or detail, "note_detail": detail if nt else ""}
        return page(request, "assets.html", ctx, kind=kind)

    def asset_job_template() -> str:
        return sch.sched.template if sch.sched else "default"

    @app.post("/assets/upload")
    async def assets_upload(request: Request, srv: str = Form(), item: str = Form(), file: UploadFile = File(...), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Bad request", status_code=400)
        lib, it = find(srv, item)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        raw = await file.read(bp.MAX_BYTES + 1)
        back_ = f"/item/{it.id}?s={srv}"
        try:
            dest = bp.save(assets_root(), bp.AssetIndex(assets_root()), it, raw)
        except (bp.AssetError, OSError) as e:
            return RedirectResponse(f"/assets?{urlencode({'s': srv, 'note': 'bad', 'detail': f'Custom poster for {it.name} refused: {e}'})}", status_code=303)
        title = f"{LABEL[srv]}: custom poster for {it.name}"
        try:
            job = jm.start(title, lambda: apply_work(srv, None, it.id, asset_job_template(), False), priority=True)
        except actions.Busy:
            return RedirectResponse(f"/assets?{urlencode({'s': srv, 'note': 'busy'})}", status_code=303)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/assets/remove")
    def assets_remove(request: Request, srv: str = Form(), item: str = Form(), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Bad request", status_code=400)
        lib, it = find(srv, item)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        p = bp.AssetIndex(assets_root()).find(it)
        if p is None:
            return RedirectResponse(f"/assets?{urlencode({'s': srv, 'note': 'nofile'})}", status_code=303)
        try:
            bp.remove(assets_root(), data, p)
        except (bp.AssetError, OSError) as e:
            return RedirectResponse(f"/assets?{urlencode({'s': srv, 'note': 'bad', 'detail': str(e)})}", status_code=303)
        return RedirectResponse(f"/assets?{urlencode({'s': srv, 'note': 'removed'})}", status_code=303)

    @app.post("/assets/apply")
    def assets_apply(request: Request, srv: str = Form(), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Bad request", status_code=400)
        job = jm.start(f"{LABEL[srv]}: use custom posters", lambda: apply_work(srv, None, None, asset_job_template(), True), interruptible=True)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/settings/assets")
    def settings_assets(request: Request, path: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        saved = load_settings(data)
        if path.strip():
            try:
                saved["assets_dir"] = bp.validate_dir(path)
            except ValueError as e:
                return back("bad", f"Custom posters folder: {e}")
        else:
            saved.pop("assets_dir", None)  # blank = the default folder
        save_settings(data, saved)
        reload_config()
        return back("assetsok")

    # ---- where backups are copied to ----------------------------------------------------------------------------
    @app.post("/settings/destination")
    def settings_destination(request: Request, type: str = Form("none"), path: str = Form(""), host: str = Form(""), port: str = Form("445"),
                             share: str = Form(""), folder: str = Form(""), username: str = Form(""), domain: str = Form(""),
                             password: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        try:
            clean = dst.validate({"type": type, "path": path, "host": host, "port": port, "share": share, "folder": folder,
                                  "username": username, "domain": domain})
            if password:
                secrets_store.save(data, dst.SMB_PASSWORD_ENV, password)  # blank keeps the saved one
        except ValueError as e:
            return back("bad", f"Backup destination: {e}")
        saved = load_settings(data)
        saved["backup_destination"] = clean
        save_settings(data, saved)
        reload_config()
        schedmod.update(data, push_error="")
        return back("destok")

    @app.post("/settings/destination/test")
    def settings_destination_test(request: Request, token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        d = dst.from_cfg(cfg)
        if d is None:
            return back("bad", "No backup destination is set.")
        try:
            return back("ok", d.check())
        except dst.DestError as e:
            return back("fail", f"{d.describe()}: {e}")

    # ---- backup and restore of laminator itself -----------------------------------------------------------------
    def bk_path(name: str) -> Path | None:
        """A backup file in <data>/backups by exact file name; anything else (paths, odd names) is refused."""
        if name != Path(name).name or not bk.NAME_RE.match(name):
            return None
        p = bk.backups_dir(data) / name
        return p if p.is_file() else None

    def ev(kind: str, name: str = "", message: str = "") -> actions.Event:
        return actions.Event(kind, name, message)

    @app.post("/settings/backup/create")
    def backup_create(request: Request, include_secrets: str = Form(""), passphrase: str = Form(""),
                      passphrase2: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        inc = include_secrets == "1"
        if inc and passphrase != passphrase2:
            return back("bkbad", "The two passphrases do not match.")
        if inc and len(passphrase) < bk.MIN_PASSPHRASE:
            return back("bkbad", f"The passphrase must be at least {bk.MIN_PASSPHRASE} characters.")

        def work():
            yield ev("info", message="creating the backup (this can take a while with many saved originals)...")
            rep = bk.create(data, None, inc, passphrase if inc else None, config_path)
            yield ev("backup", rep.path.name, f"{rep.size / 1e6:.1f} MB, {rep.files} files; keys and password "
                     f"{'included (encrypted)' if rep.has_secrets else 'not included'}")
            for kind, i in rep.servers.items():
                yield ev("info", message=f"  {LABEL.get(kind, kind)}: {i['posters']} poster record(s), {i['meta']} metadata record(s), {i['originals']} original(s)")
            dest = dst.from_cfg(cfg)
            if dest:
                try:
                    dst.push(rep.path, dest)  # a hand-made backup is copied, never pruned
                    yield ev("backup", rep.path.name, f"copied to {dest.describe()} and checked there")
                except dst.DestError as e:
                    yield ev("warn", rep.path.name, f"the local backup is fine, but the copy to {dest.describe()} failed: {e}")

        try:
            job = jm.start("Backup of laminator's data", work)
        except actions.Busy:
            return back("busy")
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/backups/download/{name}")
    def backup_download(name: str):
        p = bk_path(name)
        if p is None:
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(p, filename=p.name, media_type="application/gzip", headers={"Cache-Control": "no-store"})

    @app.post("/backups/verify")
    def backup_verify(request: Request, name: str = Form(), passphrase: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        p = bk_path(name)
        if p is None:
            return HTMLResponse("Not found", status_code=404)

        def work():
            rep = bk.verify(p, passphrase or None)
            yield ev("backup", p.name, f"OK: {rep.files} files match their checksums; nothing missing or extra")
            for n in rep.notes:
                yield ev("info", message=f"  {n}")

        try:
            job = jm.start(f"Verify {p.name}", work)
        except actions.Busy:
            return back("busy")
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.post("/backups/upload")
    async def backup_upload(request: Request, file: UploadFile = File(...), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        part = bk.backups_dir(data) / time.strftime("laminator-backup-uploaded-%Y%m%d-%H%M%S.tar.gz.part")
        size = 0
        try:
            with open(part, "wb") as out:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD:
                        raise bk.BackupError(f"the file is larger than the {MAX_UPLOAD // (1024 * 1024)} MB limit (LAMINATOR_MAX_UPLOAD_MB)")
                    out.write(chunk)
            bk.verify(part)  # names, plain files only, every checksum; the upload is useless (and refused) if it fails
        except bk.BackupError as e:
            part.unlink(missing_ok=True)
            return back("bkbad", f"Upload refused: {e}")
        part.replace(part.with_name(part.name[:-5]))
        return back("bkup")

    @app.get("/backups/restore", response_class=HTMLResponse)
    def backup_restore_page(request: Request, name: str):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        p = bk_path(name)
        if p is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            m = bk.read_manifest(p)
        except bk.BackupError as e:
            return back("bkbad", str(e))
        will_move = bk._existing(data, restoring_secrets=m.get("has_secrets", False))
        return page(request, "restore_confirm.html", {
            "name": name, "created": m.get("created", "?"), "has_secrets": bool(m.get("has_secrets")),
            "servers": [(LABEL.get(k, k), v) for k, v in m.get("servers", {}).items()], "will_move": will_move, "n_files": len(m["files"])})

    @app.post("/backups/restore")
    def backup_restore(request: Request, name: str = Form(), phrase: str = Form(""), passphrase: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        p = bk_path(name)
        if p is None:
            return HTMLResponse("Not found", status_code=404)
        if phrase.strip() != "RESTORE":
            return HTMLResponse("To restore, type RESTORE exactly. Nothing was done.", status_code=400)

        def work():
            yield ev("info", message="verifying every file before touching anything...")
            rep = bk.restore(p, data, force=True, passphrase=passphrase or None)  # forced: old files are moved aside, never deleted
            yield ev("backup", p.name, f"restored {rep.files} files")
            for n in rep.notes:
                yield ev("info", message=f"  {n}")
            reload_config()
            reconnect()
            yield ev("info", message="settings and connections reloaded")

        try:
            job = jm.start(f"Restore {p.name}", work)
        except actions.Busy:
            return back("busy")
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ---- metadata overrides: plan, edit, apply ---------------------------------------------------------------------
    OVERRIDES_MAX = 200_000

    def overrides_text() -> str:
        return overrides_file.read_text() if overrides_file.exists() else ""

    def write_overrides(text: str) -> None:
        """Atomic write; the version being replaced is kept as overrides.yaml.prev (one step of undo)."""
        if overrides_file.exists():
            shutil_copy(overrides_file, overrides_file.with_name("overrides.yaml.prev"))
        tmp = overrides_file.with_name("overrides.yaml.tmp")
        tmp.write_text(text)
        tmp.replace(overrides_file)

    def md_back(note: str, detail: str = "", kind: str | None = None):
        q = {"note": note, **({"detail": detail[:200]} if detail else {}), **({"s": kind} if kind else {})}
        return RedirectResponse(f"/metadata?{urlencode(q)}", status_code=303)

    def compute_plans(kind: str, only: str | None = None):
        entries = metamod.for_server(metamod.load_overrides(overrides_file, required=False), kind)
        return metamod.plan_all(rt.servers[kind], store_for(kind), entries, only, kind,
                                elsewhere=lambda: {k: libs(k) for k in rt.servers if k != kind})

    @app.get("/metadata", response_class=HTMLResponse)
    def metadata_page(request: Request, note: str = "", detail: str = ""):
        kind = pick(request)
        nk, text = NOTES.get(note, ("", ""))
        error, plans = None, []
        try:
            plans = compute_plans(kind)
        except metamod.OverrideError as e:
            error = str(e)
        except httpx.HTTPError as e:
            error = f"could not read from {LABEL[kind]} ({type(e).__name__}): the plan needs a working connection"
        absent = [p for p in plans if p.absent]   # titles that are only on the other server: listed apart, never counted as problems
        plans = [p for p in plans if not p.absent]
        n_writes = sum(len(p.writes) for p in plans)
        status_of = {"ok": "set", "hand-edit": "conflict", "reapply": "reverted", "lock": "lock"}
        rows, fields = [], set()
        for p in plans:  # what the page's search and filters look at, kept short
            keys = [c.key for c in p.changes]
            fields.update(keys)
            words = [p.name] + [f"{c.key} {str(c.old)[:120]} {str(c.new)[:120]}" for c in p.changes] + ([p.error] if p.error else [])
            statuses = ["error"] if p.error else sorted({status_of.get(c.action, "change") for c in p.changes})
            rows.append({"p": p, "search": " ".join(words).lower(), "status": " ".join(statuses), "fields": " ".join(keys)})
        ctx = {"absent": absent, "absent_label": ", ".join(sorted({LABEL.get(k, k) for p in absent for k in p.absent.split(", ")})),
               "rows": rows, "field_names": sorted(fields), "plans": plans, "error": error, "n_writes": n_writes, "n_conflicts": sum(any(c.action == "hand-edit" for c in p.changes) for p in plans),
               "n_items": len(plans), "has_file": overrides_file.exists(), "note_kind": nk, "note_text": text or detail,
               "note_detail": detail if text else "", "file": str(overrides_file.resolve())}
        return page(request, "metadata.html", ctx, kind=kind)

    @app.get("/metadata/edit", response_class=HTMLResponse)
    def metadata_edit(request: Request):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        text = overrides_text() or EXAMPLE_OVERRIDES
        return page(request, "overrides_edit.html", {"text": text, "error": None, "has_prev": overrides_file.with_name("overrides.yaml.prev").exists(),
                                                    "file": str(overrides_file.resolve())})

    @app.post("/metadata/edit")
    def metadata_save(request: Request, text: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        text = text.replace("\r\n", "\n")
        err = None
        if len(text) > OVERRIDES_MAX:
            err = f"the file is larger than {OVERRIDES_MAX // 1000} KB"
        else:
            try:
                metamod.parse_overrides(text)
            except metamod.OverrideError as e:
                err = str(e)
        if err:  # nothing is saved; the text stays in the box so no work is lost
            return page(request, "overrides_edit.html", {"text": text, "error": err, "file": str(overrides_file.resolve()),
                                                        "has_prev": overrides_file.with_name("overrides.yaml.prev").exists()}, status_code=400)
        write_overrides(text)
        return md_back("mdsaved")

    @app.post("/metadata/edit/undo")
    def metadata_undo(request: Request, token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        prev = overrides_file.with_name("overrides.yaml.prev")
        if not prev.exists():
            return md_back("bad", "There is no previous version.")
        write_overrides(prev.read_text())
        return md_back("mdundo")

    def _names(text: str) -> list[str]:
        """Names typed into a list field: one per line or separated by commas."""
        return [x.strip() for x in re.split(r"[,\n]", text) if x.strip()]

    @app.post("/metadata/add")
    async def metadata_add(request: Request):
        form = await request.form()
        refusal = guard(request, str(form.get("token", "")))
        if refusal:
            return refusal
        g = lambda k: str(form.get(k, "")).strip()
        srv, item = g("srv"), g("item")
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        lib, it = find(srv, item)
        if it is None:
            return HTMLResponse("Item not found", status_code=404)
        fields: dict = {}
        try:
            for key in ("rating", "title", "sort_title", "original_title", "overview", "tagline"):
                val = g(key)
                if val:
                    metamod.check_value(key, val, key)
                    fields[key] = val
                elif key in metamod.CLEARABLE and g("clear_" + key):
                    fields[key] = ""  # an empty text clears it
            if g("critic_rating"):
                if not re.fullmatch(r"\d{1,3}", g("critic_rating")):
                    raise metamod.OverrideError("critic rating: a whole number from 0 to 100")
                fields["critic_rating"] = int(g("critic_rating"))
                metamod.check_value("critic_rating", fields["critic_rating"], "critic rating")
            for key in ("tags", "genres", "studios"):
                mode, names = g(key + "_mode") or "replace", _names(g(key))
                if mode == "clear":
                    fields[key] = []
                elif names:
                    fields[key] = names if mode == "replace" else {mode: names} if mode in ("add", "remove") else None
                    if fields[key] is None:
                        raise metamod.OverrideError(f"{key}: unknown mode")
                if key in fields:
                    metamod.check_value(key, fields[key], key)
            if g("score"):
                if _NUM.match(g("score")):  # typed on the same 0-100 scale as the critic rating; the file keeps the 0-10 score
                    if not 0 <= float(g("score")) <= 100:
                        raise metamod.OverrideError("poster score: a number from 0 to 100, or short text like A+")
                    fields["score"] = round(float(g("score")) / 10, 3)
                else:
                    fields["score"] = g("score")
                metamod.check_score(fields["score"], "score")
            if g("template"):
                if g("template") not in template_names():
                    raise metamod.OverrideError(f"template: {g('template')!r} is not one of your templates")
                fields["template"] = g("template")
            if g("source"):
                if g("source") not in SOURCES:
                    raise metamod.OverrideError(f"source: {g('source')!r} is not a score source")
                fields["source"] = g("source")
            if g("no_overlay"):
                fields["overlay"] = False
        except (metamod.OverrideError, ValueError) as e:
            return md_back("mdbad", str(e), srv)
        if not fields:
            return md_back("mdbad", "Nothing to add: fill in at least one field.", srv)
        if "rating" in fields:
            try:
                allowed = rt.servers[srv].rating_names(it.id)
            except httpx.HTTPError:
                allowed = []
            if allowed and fields["rating"] not in allowed:
                return md_back("mdbad", f"{fields['rating']!r} is not a rating {LABEL[srv]} knows ({', '.join(allowed)}).", srv)
        if g("entry"):  # the item page edits the entry that already applies to this title
            try:
                idx = int(g("entry"))
                entries = metamod.parse_overrides(overrides_text())
                if metamod.matching_entry(entries, it, lib, srv) != idx:
                    raise metamod.OverrideError("the file changed since the page was opened: reload the item and try again")
                old = entries[idx]
                new_text = metamod.replace_entry(overrides_text(), idx, metamod.entry_yaml(old["match"], fields, old.get("server")))
            except (metamod.OverrideError, ValueError) as e:
                return md_back("mdbad", str(e), srv)
            write_overrides(new_text)
            return md_back("mdentry", "", srv)
        ids = {k: it.provider_ids[k] for k in ("imdb", "tmdb", "tvdb") if it.provider_ids.get(k)}
        match = dict(list(ids.items())[:1]) if ids else {"library": lib, "title": it.name, **({"year": it.year} if it.year else {})}
        server = srv if len(enabled_servers(cfg)) > 1 else None
        try:
            existing = metamod.parse_overrides(overrides_text())
        except metamod.OverrideError as e:
            return md_back("mdbad", f"overrides.yaml has a problem, fix it first: {e}", srv)
        if any(e["match"] == match and e.get("server") in (None, srv) for e in existing):
            return md_back("mddup", "", srv)
        try:
            new_text = metamod.append_entry(overrides_text(), metamod.entry_yaml(match, fields, server))
            after = metamod.parse_overrides(new_text)
            if len(after) != len(existing) + 1 or after[-1]["match"] != match:  # appended exactly one entry, the intended one
                raise metamod.OverrideError("the file's layout made the append unreliable; use the editor")
        except metamod.OverrideError as e:
            return md_back("mdbad", str(e), srv)
        write_overrides(new_text)
        return md_back("mdadded", "", srv)

    @app.get("/metadata/confirm", response_class=HTMLResponse)
    def metadata_confirm(request: Request, item: str = "", force: str = ""):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        kind = pick(request)
        try:
            plans = compute_plans(kind, item or None)
        except (metamod.OverrideError, httpx.HTTPError) as e:
            return md_back("mdbad", str(e), kind)
        todo = [p for p in plans if not p.error and (p.writes or (force == "1" and any(c.action == "hand-edit" for c in p.changes)))]
        return page(request, "meta_confirm.html", {"plans": todo, "item": item, "force": force == "1",
                                                  "n": sum(len(p.writes) + (sum(c.action == "hand-edit" for c in p.changes) if force == "1" else 0) for p in todo)}, kind=kind)

    @app.post("/metadata/apply")
    def metadata_apply(request: Request, srv: str = Form(), item: str = Form(""), force: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        kind = srv

        def work():
            ws = make_ws(kind)
            entries = metamod.for_server(metamod.load_overrides(overrides_file, required=False), kind)
            for p in metamod.plan_all(ws.srv, ws.store, entries, item or None, kind, elsewhere=lambda: {k: libs(k) for k in rt.servers if k != kind}):
                if p.absent:
                    continue   # the title is on the other server only: nothing to do here, and not a problem
                if p.error:
                    yield ev("error", p.name, p.error)
                    continue
                skipped = [c for c in p.changes if c.action == "hand-edit"]
                if not p.writes and not (force == "1" and skipped):
                    yield ev("same", p.name, "nothing to change" + (f" ({len(skipped)} field(s) edited in the server, left alone)" if skipped else ""))
                    continue
                try:
                    r = metamod.apply_item(ws.srv, ws.store, p, force == "1")
                except httpx.HTTPError as e:  # one title the server refuses must not stop the rest
                    code = e.response.status_code if getattr(e, "response", None) is not None else type(e).__name__
                    yield ev("warn", p.name, f"the server refused the update (HTTP {code}); nothing was changed for this title")
                    continue
                if r.ok:
                    yield ev("apply", p.name, ", ".join(c.key for c in p.writes) + " written and verified by reading it back")
                else:
                    for m in r.messages:
                        yield ev("warn", p.name, m)
                if skipped and force != "1":
                    yield ev("skip", p.name, f"{', '.join(c.key for c in skipped)}: edited in the server since; left alone")

        try:
            job = jm.start(f"{LABEL[kind]}: apply metadata overrides" + (f" ({item})" if item else ""), work, priority=bool(item))
        except actions.Busy:
            return md_back("busy", "", kind)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ---- choosing another poster ---------------------------------------------------------------------------------
    PER_PAGE = 24

    def candidates_for(kind: str, item_id: str):
        """(candidates, number dropped as unsafe, error text). The browser only ever receives opaque ids for them."""
        try:
            raw = rt.servers[kind].remote_images(item_id)
        except httpx.HTTPStatusError as e:
            return [], 0, f"the server answered HTTP {e.response.status_code} when asked for poster candidates"
        except httpx.HTTPError as e:
            return [], 0, f"could not ask the server for candidates ({type(e).__name__})"
        found = pst.parse_candidates(raw)
        cands.put((kind, item_id), found)
        return found, len(raw) - len(found), None

    @app.get("/posters/{item_id}", response_class=HTMLResponse)
    def posters_page(request: Request, item_id: str, p: str = "", lang: str = "", page_no: int = 1):
        kind = pick(request)
        lib, it = find(kind, item_id)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        found, dropped, error = candidates_for(kind, item_id)
        providers = sorted({c.label for c in found})
        langs = sorted({c.language or "none" for c in found}, key=lambda x: (x != "en", x))
        shown = [c for c in found if (not p or c.label == p) and (not lang or (c.language or "none") == lang)]
        pages = max(1, -(-len(shown) // PER_PAGE))
        page_no = min(max(page_no, 1), pages)
        ctx = {"custom": custom_poster_ctx(kind, it)["rel"], "it": it, "lib": lib, "cands": shown[(page_no - 1) * PER_PAGE: page_no * PER_PAGE], "total": len(shown), "all_total": len(found),
               "dropped": dropped, "error": error, "providers": providers, "langs": langs, "p": p, "lang": lang,
               "page_no": page_no, "pages": pages}
        return page(request, "posters.html", ctx, kind=kind)

    def candidate_or_none(kind: str, item_id: str, cid: str):
        return cands.get((kind, item_id), cid)

    @app.get("/posters/thumb/{item_id}/{cid}")
    def poster_thumb(request: Request, item_id: str, cid: str):
        kind = pick(request)
        c = candidate_or_none(kind, item_id, cid) if kind else None
        if c is None:
            return Response(status_code=404)
        try:
            raw = pst.fetch_image(c.thumb_url, data / "cache" / "posters")
            return Response(pst.thumbnail(raw), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})
        except pst.PosterError as e:
            return Response(f"unavailable: {e}", status_code=502)

    @app.get("/posters/preview/{item_id}/{cid}")
    def poster_preview(request: Request, item_id: str, cid: str):
        kind = pick(request)
        c = candidate_or_none(kind, item_id, cid) if kind else None
        lib, it = find(kind, item_id) if kind else (None, None)
        if c is None or it is None:
            return Response(status_code=404)
        try:
            raw = pst.fetch_image(pst.mid_size(c.url), data / "cache" / "posters")
            p = dict(request.query_params)
            tpl, _ = assets.load_template(p.get("name", "default"), data)
            picked = actions.choose_score(it, rt.mdb, tpl, score_overrides(kind)) if rt.mdb else None
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            img.thumbnail((600, 900))
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=90)
            return Response(render(buf.getvalue(), picked[1] if picked else "7.3", tpl), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})
        except (pst.PosterError, Exception) as e:  # a bad candidate must not crash the page
            return Response(f"unavailable: {type(e).__name__}", status_code=502)

    @app.get("/posters/use/{item_id}/{cid}", response_class=HTMLResponse)
    def poster_use_page(request: Request, item_id: str, cid: str):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        kind = pick(request)
        c = candidate_or_none(kind, item_id, cid) if kind else None
        lib, it = find(kind, item_id) if kind else (None, None)
        if c is None or it is None:
            return RedirectResponse(f"/posters/{item_id}?s={kind}", status_code=303)  # expired: reload the list
        return page(request, "poster_use.html", {"it": it, "c": c, "managed": store_for(kind).get(it.id) is not None}, kind=kind)

    @app.post("/posters/use")
    def poster_use(request: Request, srv: str = Form(), item: str = Form(), cid: str = Form(), template: str = Form("default"),
                   token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        c = candidate_or_none(srv, item, cid)
        lib, it0 = find(srv, item)
        if c is None or it0 is None:
            return RedirectResponse(f"/posters/{item}?s={srv}", status_code=303)
        try:
            assets.template_file(template, data)
        except FileNotFoundError:
            return HTMLResponse("Template not found", status_code=404)

        def work():
            ws = make_ws(srv)
            fresh = ws.srv.items_by_ids([item])
            if not fresh:
                raise RuntimeError("the item no longer exists on the server")
            it = fresh[0]
            # 1) keep the CURRENT poster as the original BEFORE anything is replaced (a no-op if we already manage this item)
            _, _, notes = actions.get_original(ws.srv, ws.store, ws.originals, it)
            yield from notes
            yield ev("info", message=f"current poster kept as the original; asking {LABEL[srv]} to fetch the {c.label} candidate...")
            before = it.poster_tag
            ws.srv.download_remote_image(it.id, c.url, c.provider)
            for _ in range(POSTER_WAIT_TRIES):  # the server fetches in the request, but its image tag can lag a moment
                now = ws.srv.items_by_ids([item])[0]
                if now.poster_tag != before:
                    break
                time.sleep(POSTER_WAIT_STEP)
            else:
                raise RuntimeError("the server did not change the poster (it may have refused that image)")
            yield ev("poster", it.name, f"{c.label} {c.width}x{c.height}{' ' + c.language if c.language else ''} is now the poster")
            tpl, sig = assets.load_template(template, data)
            yield from actions.apply_posters(ws, tpl, sig, overrides_file, False, item=it.id,
                                             critic=bool(cfg.get("critic_rating", False)), only_changed=False)

        job = jm.start(f"{LABEL[srv]}: new poster for {it0.name}", work, priority=True)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ---- poster history: go back to an earlier poster ----------------------------------------------------------------
    def hist_file(kind: str, item_id: str, name: str) -> Path | None:
        """One of this item's saved posters, by exact file name (nothing else under the originals folder is reachable)."""
        if not re.fullmatch(rf"{re.escape(item_id)}(-[0-9a-f]{{8}})?\.img", name):
            return None
        f = sdir(kind) / "originals" / name
        return f if f.is_file() and not f.is_symlink() else None

    @app.get("/history/{item_id}", response_class=HTMLResponse)
    def history_page(request: Request, item_id: str):
        kind = pick(request)
        lib, it = find(kind, item_id)
        if it is None:
            return HTMLResponse("Not found", status_code=404)
        row = store_for(kind).get(it.id)
        entries = actions.poster_history(sdir(kind) / "originals", it.id, row["original_sha"] if row else None)
        previous = next((e for e in entries if not e["current"]), None)
        first = entries[-1] if entries and not entries[-1]["current"] else None
        for e in entries:
            e["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"]))
        return page(request, "history.html", {"it": it, "lib": lib, "entries": entries, "previous": previous, "first": first,
                                              "managed": row is not None}, kind=kind)

    @app.get("/history/thumb/{item_id}/{name}")
    def history_thumb(request: Request, item_id: str, name: str):
        kind = pick(request)
        f = hist_file(kind, item_id, name) if kind else None
        if f is None:
            return Response(status_code=404)
        try:
            return Response(pst.thumbnail(f.read_bytes()), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})
        except Exception:
            return Response("unreadable image", status_code=502)

    @app.get("/history/preview/{item_id}/{name}")
    def history_preview(request: Request, item_id: str, name: str):
        kind = pick(request)
        f = hist_file(kind, item_id, name) if kind else None
        lib, it = find(kind, item_id) if kind else (None, None)
        if f is None or it is None:
            return Response(status_code=404)
        try:
            tpl, _ = assets.load_template(request.query_params.get("name", "default"), data)
            picked = actions.choose_score(it, rt.mdb, tpl, score_overrides(kind)) if rt.mdb else None
            img = Image.open(f).convert("RGB")
            img.thumbnail((600, 900))
            buf = io.BytesIO(); img.save(buf, "JPEG", quality=90)
            return Response(render(buf.getvalue(), picked[1] if picked else "7.3", tpl), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=300"})
        except Exception as e:
            return Response(f"unavailable: {type(e).__name__}", status_code=502)

    @app.get("/history/use/{item_id}/{name}", response_class=HTMLResponse)
    def history_use_page(request: Request, item_id: str, name: str):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        kind = pick(request)
        f = hist_file(kind, item_id, name) if kind else None
        lib, it = find(kind, item_id) if kind else (None, None)
        if f is None or it is None:
            return HTMLResponse("Not found", status_code=404)
        row = store_for(kind).get(it.id)
        entries = actions.poster_history(sdir(kind) / "originals", it.id, row["original_sha"] if row else None)
        entry = next((e for e in entries if e["name"] == name or e["sha"] == actions.sha(f.read_bytes())), None)
        if entry:
            entry["when"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry["mtime"]))
        return page(request, "history_use.html", {"it": it, "name": name, "entry": entry,
                                                 "is_current": bool(entry and entry["current"])}, kind=kind)

    @app.post("/history/use")
    def history_use(request: Request, srv: str = Form(), item: str = Form(), name: str = Form(), template: str = Form("default"),
                    token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        f0 = hist_file(srv, item, name)
        lib, it0 = find(srv, item)
        if f0 is None or it0 is None:
            return HTMLResponse("Not found", status_code=404)
        try:
            assets.template_file(template, data)
        except FileNotFoundError:
            return HTMLResponse("Template not found", status_code=404)

        def work():
            ws = make_ws(srv)
            fresh = ws.srv.items_by_ids([item])
            if not fresh:
                raise RuntimeError("the item no longer exists on the server")
            it = fresh[0]
            f = hist_file(srv, item, name)
            if f is None:
                raise RuntimeError("that saved poster is gone")
            picture = f.read_bytes()
            Image.open(io.BytesIO(picture)).verify()  # a damaged file must never be uploaded
            row, _, notes = actions.get_original(ws.srv, ws.store, ws.originals, it)  # also makes sure the item is managed
            yield from notes
            tpl, sig = assets.load_template(template, data)
            if actions.sha(picture) == row["original_sha"]:
                yield ev("info", message="that is already the current original; re-checking the overlay")
            else:
                ws.srv.upload_primary(it.id, picture, actions._ctype(picture))
                yield ev("poster", it.name, f"saved poster {name} is the poster again")
            yield from actions.apply_posters(ws, tpl, sig, overrides_file, False, item=it.id,
                                             critic=bool(cfg.get("critic_rating", False)), only_changed=False)

        job = jm.start(f"{LABEL[srv]}: go back to an earlier poster for {it0.name}", work, priority=True)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ---- export: posters laid out next to the media ---------------------------------------------------------------------
    @app.get("/export", response_class=HTMLResponse)
    def export_page(request: Request, note: str = "", detail: str = ""):
        kind = pick(request)
        nk, text = NOTES.get(note, ("", ""))
        return page(request, "export.html", {"libraries": sorted(libs(kind)), "exports": exp.list_exports(data), "note_kind": nk,
                                             "note_text": text or detail, "note_detail": detail if text else ""}, kind=kind)

    @app.post("/export/create")
    def export_create(request: Request, srv: str = Form(), which: str = Form("current"), layout: str = Form("location"),
                      naming: str = Form("auto"), library: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        if which not in exp.WHICH or layout not in exp.LAYOUTS or naming not in exp.NAMINGS:
            return HTMLResponse("Bad request", status_code=400)
        if library and library not in libs(srv):
            return HTMLResponse("Library not found", status_code=404)

        def work():
            ws = make_ws(srv)
            yield from exp.run_export(ws, data, which, layout, naming, library or None, None, LABEL[srv])

        job = jm.start(f"{LABEL[srv]}: export posters" + (f" ({library})" if library else ""), work)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    @app.get("/exports/download/{name}")
    def export_download(name: str):
        f = exp.exports_dir(data) / name
        if name != Path(name).name or not exp.NAME_RE.match(name) or not f.is_file() or f.is_symlink():
            return HTMLResponse("Not found", status_code=404)
        return FileResponse(f, filename=name, media_type="application/zip", headers={"Cache-Control": "no-store"})

    # ---- cleanup: what laminator no longer needs --------------------------------------------------------------------
    @app.get("/cleanup", response_class=HTMLResponse)
    def cleanup_page(request: Request, note: str = "", detail: str = ""):
        kind = pick(request)
        nk, text = NOTES.get(note, ("", ""))
        try:
            lim = cln.Limits.from_cfg(cfg)
        except ConfigError as e:
            return md_cleanup_error(request, kind, str(e))
        ws = make_ws(kind)
        now = time.time()
        sp, rp = cln.scan_server(ws, now, lim, mark=False), cln.scan_root(data, now, lim)  # looking never marks or removes anything
        fmt = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
        ctx = {"sp": sp, "rp": rp, "lim": lim, "note_kind": nk, "note_text": text or detail, "note_detail": detail if text else "",
               "gone": [{"name": g.name, "since": fmt(g.since), "files": len(g.files), "mb": round(g.bytes / 1e6, 1)} for g in sp.gone],
               "waiting": [{"name": n, "days": d} for _, n, d in sp.waiting],
               "rows": [(label, len(items), round(sum(cln.size(p) for p in items) / 1e6, 1), what) for label, items, what in (
                   ("Saved posters nothing refers to", sp.orphans, "moved to trash"),
                   ("Old dry-run renders", sp.outputs, "deleted"),
                   ("Old poster-preview and MDBList caches", rp.caches, "deleted"),
                   ("Old export packages", rp.exports, "deleted"),
                   ("Trash older than the retention", sp.trash + rp.trash, "deleted for good"))],
               "nothing": sp.empty and rp.empty and not sp.waiting}
        return page(request, "cleanup.html", ctx, kind=kind)

    def md_cleanup_error(request, kind, msg):
        return page(request, "cleanup.html", {"sp": cln.Plan(problem=msg, server_ok=False), "rp": cln.Plan(), "lim": cln.Limits(), "gone": [], "waiting": [],
                                              "rows": [], "nothing": False, "note_kind": "bad", "note_text": msg, "note_detail": ""}, kind=kind)

    @app.post("/cleanup/run")
    def cleanup_run(request: Request, srv: str = Form(), safety: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        try:
            lim = cln.Limits.from_cfg(cfg, 180 if safety == "1" else None)
        except ConfigError as e:
            return HTMLResponse(str(e), status_code=400)

        def work():
            yield from cln.run(make_ws(srv), lim=lim)

        job = jm.start(f"{LABEL[srv]}: clean up", work)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ---- the trash: put cleaned-up things back ---------------------------------------------------------------------
    @app.get("/trash", response_class=HTMLResponse)
    def trash_page(request: Request):
        kind = pick(request)
        try:
            lim = cln.Limits.from_cfg(cfg)
        except ConfigError:
            lim = cln.Limits()
        batches = trs.list_batches(make_ws(kind), time.time(), lim)
        fmt = lambda t: time.strftime("%Y-%m-%d", time.localtime(t)) if t else "?"
        view = [{"stamp": b.stamp, "scope": b.scope, "mb": round(b.bytes / 1e6, 1), "left": max(b.keep_days_left, 0),
                 "entries": [{"id": i.item_id, "name": i.name, "files": len(i.files), "since": fmt(i.missing_since)} for i in b.items],
                 "orphans": len(b.orphans), "safety": [d.name for d in b.safety]} for b in batches]
        return page(request, "trash.html", {"batches": view, "trash_days": lim.trash_days}, kind=kind)

    @app.post("/trash/restore")
    def trash_restore(request: Request, srv: str = Form(), stamp: str = Form(), item: str = Form(""), everything: str = Form(""),
                      token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers or not trs.STAMP_RE.match(stamp):
            return HTMLResponse("Bad request", status_code=400)
        if not item and everything != "1":
            return HTMLResponse("Say what to restore.", status_code=400)
        if item and not re.fullmatch(r"[A-Za-z0-9]{1,64}", item):
            return HTMLResponse("Bad request", status_code=400)

        def work():
            yield from trs.restore(make_ws(srv), stamp, item or None)

        job = jm.start(f"{LABEL[srv]}: restore from trash {stamp}" + (f" ({item})" if item else ""), work)
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    # ---- two-factor sign-in and sessions --------------------------------------------------------------------------
    def password_check(request: Request, current: str) -> str | None:
        """None when `current` is the interface password, else the NOTES key to send back (rate-limited like sign-in)."""
        who = client_ip(request)
        if limiter.blocked(who):
            return "pwlimit"
        if not login_ok(current):
            limiter.fail(who)
            audit.log(data, "wrong password (settings)", ip=who)
            return "pwwrong"
        limiter.ok(who)
        return None

    def mfa_page(request: Request, ctx: dict, status_code: int = 200):
        resp = page(request, "mfa.html", ctx, status_code=status_code)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.post("/settings/mfa/start")
    def settings_mfa_start(request: Request, current: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        bad = password_check(request, current)
        if bad:
            return back(bad)
        if mfa.enabled(data):
            return RedirectResponse("/settings/mfa", status_code=303)
        mfa.start(data)
        return RedirectResponse("/settings/mfa", status_code=303)

    @app.get("/settings/mfa", response_class=HTMLResponse)
    def settings_mfa_page(request: Request, bad: str = ""):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        if mfa.enabled(data):
            return mfa_page(request, {"state": "on", "left": mfa.recovery_left(data), "error": MFA_ERRORS.get(bad, "")})
        sec_ = mfa.pending(data)
        if not sec_:
            return RedirectResponse("/settings#security", status_code=303)
        uri = mfa.otpauth_uri(sec_)
        key = " ".join(sec_[i:i + 4] for i in range(0, len(sec_), 4))
        return mfa_page(request, {"state": "setup", "qr": mfa.qr_data_uri(uri), "key": key, "error": MFA_ERRORS.get(bad, "")})

    @app.post("/settings/mfa/confirm")
    def settings_mfa_confirm(request: Request, code: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        codes = mfa.confirm(data, code[:20])
        if codes is None:
            audit.log(data, "two-factor setup: wrong code", ip=client_ip(request))
            return RedirectResponse("/settings/mfa?bad=code", status_code=303)
        signed_in_again(None, request, "two-factor sign-in turned on")
        return mfa_page(request, {"state": "codes", "codes": codes, "fresh": True})

    @app.post("/settings/mfa/disable")
    def settings_mfa_disable(request: Request, current: str = Form(""), code: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        bad = password_check(request, current)
        if bad:
            return RedirectResponse(f"/settings/mfa?bad={bad}", status_code=303)
        if mfa.verify(data, code[:64]) is None:
            limiter.fail(client_ip(request))
            return RedirectResponse("/settings/mfa?bad=code", status_code=303)
        mfa.disable(data)
        return signed_in_again(back("mfaoff"), request, "two-factor sign-in turned off")

    @app.post("/settings/mfa/recovery")
    def settings_mfa_recovery(request: Request, current: str = Form(""), code: str = Form(""), token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        bad = password_check(request, current)
        if bad:
            return RedirectResponse(f"/settings/mfa?bad={bad}", status_code=303)
        if mfa.verify(data, code[:64]) is None:
            limiter.fail(client_ip(request))
            return RedirectResponse("/settings/mfa?bad=code", status_code=303)
        codes = mfa.new_recovery_codes(data)
        audit.log(data, "new recovery codes", ip=client_ip(request))
        return mfa_page(request, {"state": "codes", "codes": codes, "fresh": False})

    @app.post("/settings/sessions/end")
    def settings_sessions_end(request: Request, token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        n = store.delete_others(request.cookies.get(auth.COOKIE))
        audit.log(data, "other sessions signed out", ip=client_ip(request), ended=n)
        return back("sessionsended", f"Signed out {n} other session{'' if n == 1 else 's'}.")

    # ---- the interface password -----------------------------------------------------------------------
    @app.post("/settings/password")
    def settings_password(request: Request, current: str = Form(""), new: str = Form(""), confirm: str = Form(""),
                          token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        who = client_ip(request)
        if limiter.blocked(who):
            return back("pwlimit")
        if not login_ok(current):
            limiter.fail(who)
            return back("pwwrong")
        if new != confirm:
            return back("pwmismatch")
        err = passwords.policy_error(new)
        if err:
            return back("bad", f"Password: {err}.")
        limiter.ok(who)
        passwords.save(data, new)
        return signed_in_again(back("pwok"), request, "password changed")

    @app.post("/settings/password/reset")
    def settings_password_reset(request: Request, token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if password is None:  # nothing to fall back to: removing the saved hash would lock you out
            return back("pwnoreset")
        passwords.remove(data)
        return signed_in_again(back("pwreset"), request, "saved password removed")

    def signed_in_again(resp, request: Request | None = None, why: str = "password changed"):
        """End every other session; this browser stays signed in."""
        keep = request.cookies.get(auth.COOKIE) if request is not None else None
        n = store.delete_others(keep)
        audit.log(data, why, ip=client_ip(request) if request is not None else "", other_sessions_ended=n)
        return resp

    # ---- apply / restore as jobs (the only other routes that write) -------------------------------------------------
    def total_items(kind: str, lib: str | None) -> int:
        return len(libs(kind).get(lib, [])) if lib else sum(len(v) for v in libs(kind).values())

    @app.get("/confirm", response_class=HTMLResponse)
    def confirm(request: Request, action: str, scope: str, item: str = "", lib: str = "", what: str = "all",
                template: str = "default", changed: str = "1", q: str = "", show: str = "", smin: str = "", smax: str = "",
                ssrc: str = "aggregate", ymin: str = "", ymax: str = "", crate: str = "", view: str = "", per: str = ""):
        if not can_write:
            return HTMLResponse("The UI is read-only without a password.", status_code=403)
        kind = pick(request)
        if action not in ("apply", "restore", "maintain") or scope not in ("item", "library", "all", "selection") or (action == "restore" and scope == "library") \
                or (action == "maintain" and scope != "all") or (scope == "selection" and action not in ("apply", "restore")):
            return HTMLResponse("Bad request", status_code=400)
        if scope == "selection":
            if lib not in libs(kind):
                return HTMLResponse("Library not found", status_code=404)
            try:
                assets.template_file(template, data)
            except FileNotFoundError:
                return HTMLResponse("Template not found", status_code=404)
            show, f = parse_filters(show, smin, smax, ssrc, ymin, ymax, crate)
            chosen = listing(kind, lib, q, show, f)[0][:SELECTION_MAX]
            desc = "; ".join(describe_filters(show, f) + ([f"search '{q.strip()[:30]}'"] if q.strip() else []))
            back = "/library?" + filter_query(kind, lib, q, view or "posters", per or "scroll", show, f)
            plural = "" if len(chosen) == 1 else "s"
            if action == "restore":
                title = f"Restore the original poster of {len(chosen)} selected title{plural} on {LABEL[kind]}"
                effect = (f"Puts the saved original poster back for exactly the titles listed below, so the score badge disappears, and removes the CriticRating "
                          f"laminator wrote for them. Content ratings, sort titles and other metadata overrides are not touched. A title laminator never changed is skipped.")
                what = "scores"
            else:
                title = f"Apply the score overlay (template {template}) to {len(chosen)} selected title{plural} on {LABEL[kind]}"
                effect = f"Uploads new posters to your {LABEL[kind]} server for exactly the titles listed below. Originals are saved first and can be restored." + (
                    " Also writes the score into CriticRating and locks it." if cfg.get("critic_rating") else "")
            ctx = {"title": title, "effect": effect,
                   "n": len(chosen), "action": action, "scope": "selection", "item": "", "lib": lib, "what": what, "template": template,
                   "needs_phrase": False, "phrase": PHRASE, "changed_default": changed == "1", "ids": ",".join(it.id for it in chosen),
                   "desc": desc, "names": [it.name for it in chosen[:15]], "more": max(0, len(chosen) - 15), "back": back}
            if not chosen:
                return HTMLResponse("No titles match these filters, so there is nothing to apply.", status_code=400)
            return page(request, "confirm.html", ctx, kind=kind)
        name = None
        if scope == "item":
            found = find(kind, item)[1]
            if found is None:
                return HTMLResponse("Item not found", status_code=404)
            name = found.name
        if scope == "library" and lib not in libs(kind):
            return HTMLResponse("Library not found", status_code=404)
        n = 1 if scope == "item" else total_items(kind, lib if scope == "library" else None)
        where = LABEL[kind]
        what_txt = {"posters": "the original poster", "meta": "the original metadata", "all": "the original poster and metadata"}[what if what in ("posters", "meta", "all") else "all"]
        if action == "maintain":
            title = f"Run the full maintenance on {where}"
            effect = ("Re-reads every score (a change of 0.1 or more updates the poster and CriticRating together), then cleans up what is no longer "
                      "needed: records of items that have been gone from the server past the grace period go to trash/ (never straight to deletion), "
                      "plus old renders, caches and export packages. See the Cleanup page for the exact plan.")
        elif action == "apply":
            title = f"Apply the score overlay (template {template}) to {name or (lib if scope == 'library' else f'EVERY item on {where}')} on {where}"
            effect = f"Uploads new posters to your {where} server. Originals are saved first and can be restored." + (
                " Also writes the score into CriticRating and locks it." if cfg.get("critic_rating") else "")
        else:
            title = f"Restore {what_txt} of {name or f'EVERY item laminator changed on {where}'}"
            effect = f"Puts the saved originals back on your {where} server and removes the locks laminator added."
        ctx = {"title": title, "effect": effect, "n": n, "action": action, "scope": scope, "item": item, "lib": lib,
               "what": what, "template": template, "needs_phrase": scope == "all", "phrase": PHRASE,
               "changed_default": changed != "0"}
        return page(request, "confirm.html", ctx, kind=kind)

    @app.post("/jobs/start")
    def jobs_start(request: Request, action: str = Form(), scope: str = Form(), item: str = Form(""), lib: str = Form(""),
                   what: str = Form("all"), template: str = Form("default"), phrase: str = Form(""), token: str = Form(""),
                   changed: str = Form(""), srv: str = Form(""), ids: str = Form(""), desc: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        if srv not in rt.servers:
            return HTMLResponse("Unknown or unconnected server", status_code=400)
        kind = srv
        if action not in ("apply", "restore", "maintain") or scope not in ("item", "library", "all", "selection") or (what not in ("posters", "meta", "all") and not (scope == "selection" and what == "scores")) \
                or (action == "restore" and scope == "library") or (action == "maintain" and scope != "all") or (scope == "selection" and action not in ("apply", "restore")) \
                or (scope == "selection" and action == "restore" and what != "scores"):
            return HTMLResponse("Bad request", status_code=400)
        chosen_ids: set[str] | None = None
        if scope == "selection":
            known = {it.id for v in libs(kind).values() for it in v}
            asked = [i for i in ids.split(",") if i]
            chosen_ids = {i for i in asked if i in known}
            if not chosen_ids or len(asked) > SELECTION_MAX:
                return HTMLResponse("The selection is empty or too large. Go back to the Library and choose again.", status_code=400)
        if scope == "all" and phrase.strip() != PHRASE:
            return HTMLResponse(f"To touch every item, type {PHRASE} exactly. Nothing was done.", status_code=400)
        if scope == "item" and find(kind, item)[1] is None:
            return HTMLResponse("Item not found", status_code=404)
        if scope == "library" and lib not in libs(kind):
            return HTMLResponse("Library not found", status_code=404)
        if action in ("apply", "maintain"):
            try:
                assets.template_file(template, data)
            except FileNotFoundError:
                return HTMLResponse("Template not found", status_code=404)
        target = item if scope == "item" else None
        label = re.sub(r"[^\w .,;:()'+\-]", "", desc)[:100]  # display only: what the selection was, so two selections do not look the same
        title = f"{LABEL[kind]}: {action} {scope}" + (f": {find(kind, item)[1].name}" if scope == "item" else f": {lib}" if scope == "library"
                                                       else f": {len(chosen_ids)} titles" + (f" ({label})" if label else "") if scope == "selection" else "")

        def work():
            if action == "maintain":
                yield from maintenance_work(kind, template)
            elif action == "apply":
                yield from apply_work(kind, lib if scope == "library" else None, target, template, changed == "1", chosen_ids)
            elif scope == "selection":  # restore: the original posters and the automatic CriticRating of these titles, nothing else
                ws = make_ws(kind)
                yield from actions.restore_posters(ws, None, only_ids=chosen_ids)
                yield from actions.restore_metadata(ws, None, only_ids=chosen_ids, keys={"critic_rating"})
            else:
                ws = make_ws(kind)
                if what in ("meta", "all"):
                    yield from actions.restore_metadata(ws, target)
                if what in ("posters", "all"):
                    yield from actions.restore_posters(ws, target)

        job = jm.start(title, work, priority=scope == "item" and action != "maintain",
                       interruptible=action == "maintain" or (action == "apply" and scope != "item"))
        return RedirectResponse(f"/jobs/{job.id}", status_code=303)

    def runs_ctx() -> dict:
        jobs = jm.recent()
        return {"jobs": jobs, "phrase": PHRASE, "active": any(j.status in ("running", "queued") for j in jobs)}

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request):
        return page(request, "jobs.html", runs_ctx())

    @app.get("/jobs/table", response_class=HTMLResponse)
    def jobs_table(request: Request):
        """Just the table of runs, which the Runs page asks for again every few seconds (faster while something is running)."""
        return page(request, "_jobs_table.html", runs_ctx())

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str):
        job = jm.get(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        return page(request, "job.html", job_ctx(job))

    @app.get("/jobs/{job_id}/log", response_class=HTMLResponse)
    def job_log(request: Request, job_id: str):
        job = jm.get(job_id)
        if job is None:
            return HTMLResponse("Not found", status_code=404)
        return page(request, "_joblog.html", job_ctx(job))

    def job_ctx(job) -> dict:
        status, events, error = jm.snapshot(job)
        ahead = jm.running()
        return {"job": job, "status": status, "events": events, "error": error, "position": jm.position(job),
                "ahead": ahead.title if ahead and ahead is not job else "", "can_cancel": can_write}

    @app.post("/jobs/{job_id}/cancel")
    def job_cancel(request: Request, job_id: str, token: str = Form("")):
        refusal = guard(request, token)
        if refusal:
            return refusal
        jm.cancel(job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    # ---- images (proxied; the API key stays here) -----------------------------------------------------------------
    THUMB_WIDTH = 300  # the grid shows posters at most ~200 px wide; 300 keeps them sharp on a high-density screen at a fraction of the bytes

    def thumb_of(kind: str, it, client) -> bytes:
        """A small JPEG of the poster the server shows now. Made once, kept on disk under the poster's version tag, so the server
        is asked once per poster (and again only when the poster changes); a changed poster's old thumbnail is removed."""
        d = data / "cache" / "thumbs"
        tag = re.sub(r"[^A-Za-z0-9_-]", "_", it.poster_tag or "none")[:60]
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(it.id))[:60]
        f = d / f"{kind}-{safe_id}-{tag}.jpg"
        if f.is_file():
            return f.read_bytes()
        raw = client.download_primary(it.id, max_width=420)[0]
        out = pst.thumbnail(raw, THUMB_WIDTH)
        try:
            d.mkdir(parents=True, exist_ok=True)
            for old in d.glob(f"{kind}-{safe_id}-*.jpg"):
                old.unlink(missing_ok=True)
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(out)
            tmp.replace(f)
        except OSError:
            pass  # a read-only or full disk only costs the saving, never the picture
        return out

    @app.get("/img/{item_id}/{what}")
    def image(request: Request, item_id: str, what: str):
        kind = pick(request)
        lib, it = find(kind, item_id) if kind else (None, None)
        if it is None or what not in ("current", "original", "preview", "thumb"):
            return Response(status_code=404)
        try:
            client = rt.servers[kind]
            if what == "current":
                data_b = client.download_primary(it.id, max_width=420)[0]
                return Response(data_b, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})
            if what == "thumb":
                data_b = thumb_of(kind, it, client)
                versioned = request.query_params.get("v") == (it.poster_tag or "none")  # the address names this exact poster
                return Response(data_b, media_type="image/jpeg", headers={
                    "Cache-Control": "private, max-age=31536000, immutable" if versioned else "private, max-age=60"})
            elif what == "original":
                row = store_for(kind).get(it.id)
                f = actions.original_file(row, sdir(kind) / "originals") if row else None
                if f is None or not f.exists():
                    data_b = client.download_primary(it.id, max_width=420)[0]
                else:
                    img = Image.open(f).convert("RGB"); img.thumbnail((420, 630))
                    buf = io.BytesIO(); img.save(buf, "JPEG", quality=88); data_b = buf.getvalue()
            else:
                p = dict(request.query_params)
                tpl, _ = assets.load_template(p.get("name", "default"), data)
                tpl = tuned(request.query_params, tpl)
                text = (p.get("score") or "").strip()[: metamod.MAX_SCORE_TEXT]
                if not text:
                    picked = actions.choose_score(it, rt.mdb, tpl, score_overrides(kind)) if rt.mdb else None
                    text = picked[1] if picked else "7.3"
                data_b = preview_jpeg(kind, it, tpl, text)
        except Exception as e:  # a broken server or missing font must not crash the page
            return Response(f"image unavailable: {type(e).__name__}", status_code=502)
        return Response(data_b, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})

    # ---- status ---------------------------------------------------------------------------------------------------
    def schedule_info():
        s = sch.sched
        if not s:
            return None
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"
        info = {"active": sch.obj is not None and sch.obj.is_alive(),
                "changed": f"every {s.changed_every_minutes} min" if s.changed_every_minutes else "off",
                "template": s.template, "error": sch.obj.last_error if sch.obj else None,
                "runs": [{"label": LABEL[k], "scheduled": schedmod.scheduled_on(sdir(k)), "last_changed": fmt(schedmod.load(sdir(k)).get("last_changed")),
                          "last_full": fmt(schedmod.load(sdir(k)).get("last_full"))} for k in rt.servers]}
        st_root = schedmod.load(data)
        last_b = st_root.get("last_backup")
        info["backup"] = "off" if s.backup_every_days <= 0 else f"every {s.backup_every_days} day(s), keep the last {s.backup_keep}"
        info["last_backup"] = fmt(last_b)
        d_ = dst.from_cfg(cfg)
        info["destination"] = d_.describe() if d_ else ""
        info["dest_error"] = st_root.get("push_error", "") if d_ else ""
        if s.maintenance_day is None:
            info["maintenance"], info["next"] = "off", None
        else:
            info["maintenance"] = f"monthly on day {s.maintenance_day} at {s.maintenance_time}"
            info["next"] = schedmod.next_occurrence(datetime.now(), s.maintenance_day, s.maintenance_time).strftime("%Y-%m-%d %H:%M")
        return info

    @app.get("/status", response_class=HTMLResponse)
    def status(request: Request):
        rows = []
        for kind in KINDS:
            conf = servers(cfg).get(kind)
            if not conf:
                continue
            d = sdir(kind)
            files = list((d / "originals").glob("*.img")) if (d / "originals").is_dir() else []
            posters, metas = state_counts(d)
            rows.append({"label": LABEL[kind], "url": conf.get("url") or "(no address)", "enabled": conf.get("enabled", True),
                         "state": connection_state(kind) if conf.get("enabled", True) else ("plain", "switched off"),
                         "libraries": {n: len(v) for n, v in libs(kind).items()} if kind in rt.servers else {},
                         "key": key_row(server_key_env(cfg, kind), f"{LABEL[kind]} API key"),
                         "posters": posters, "metas": metas, "originals": len(files),
                         "orig_mb": round(sum(f.stat().st_size for f in files) / 1e6, 1)})
        ctx = {"rows": rows, "mdb_key": key_row(MDBLIST_ENV, "MDBList API key"), "data": str(data.resolve()),
               "fonts": assets.list_fonts(data), "templates": template_names(), "critic": bool(cfg.get("critic_rating", False)),
               "mdb_left": getattr(rt.mdb, "remaining", None) if rt.mdb else None, "sched": schedule_info()}
        ctx["security"] = {"mfa": mfa.enabled(data), "mfa_left": mfa.recovery_left(data), "sessions": len(store.active()),
                           "proxies": os.environ.get("LAMINATOR_TRUSTED_PROXIES", "").strip(), "public_url": public.geturl() if public.netloc else "",
                           "https": is_https(request), "short_password": bool(password and len(password) < 12 and passwords.load(data) is None)}
        ctx["audit"] = [{"when": e.get("t", 0), "event": e.get("event", ""), "ip": e.get("ip", ""),
                         "more": ", ".join(f"{k}: {v}" for k, v in e.items() if k not in ("t", "event", "ip"))} for e in audit.tail(data, 25)]
        return page(request, "status.html", ctx)

    return app
