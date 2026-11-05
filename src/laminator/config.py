"""Config loading. Secrets come from the environment only, never from tracked files."""
from __future__ import annotations

import copy
import ipaddress
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from urllib.parse import urlparse


class ConfigError(Exception):
    pass


# One variable per service, named after the service. The admin password for the web UI is LAMINATOR_ADMIN_PASSWORD.
KEY_ENV = {"emby": "API_EMBY", "jellyfin": "API_JELLYFIN"}
MDBLIST_ENV = "API_MDBLIST"
_OLD = {"API_EMBY": "LAMINATOR_SERVER_KEY", "API_JELLYFIN": "LAMINATOR_JELLYFIN_KEY", "API_MDBLIST": "MDBLIST_API_KEY"}


def require_env(name: str, what: str) -> str:
    val = os.environ.get(name)
    if val:
        return val
    old = _OLD.get(name)
    if old and os.environ.get(old):
        raise ConfigError(f"{old} was renamed to {name}: rename it in .env or the environment")
    raise ConfigError(f"set {name} ({what}) in .env or the environment")


SECRET_NAMES = ("LAMINATOR_ADMIN_PASSWORD", "API_EMBY", "API_JELLYFIN", "API_MDBLIST", "LAMINATOR_SMB_PASSWORD", "LAMINATOR_BACKUP_PASSPHRASE")


def load_secret_files(env=None) -> list[str]:
    """Docker secrets: NAME_FILE=/run/secrets/x puts the file's contents into NAME (only when NAME itself is not set), so a password or key
    never has to appear in the container's environment (where `docker inspect` shows it). Returns the names that were read."""
    env = os.environ if env is None else env
    done = []
    for name in SECRET_NAMES:
        path = env.get(name + "_FILE")
        if path and not env.get(name):
            try:
                value = Path(path).read_text().strip("\r\n")
            except OSError as e:
                raise ConfigError(f"{name}_FILE={path}: cannot read it ({e.strerror or type(e).__name__})")
            if value:
                env[name] = value
                done.append(name)
    return done


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env into os.environ without overriding real env vars.

    Warns (to stderr) if the file is readable by group/others, since it holds secrets.
    """
    import sys

    path = path or Path(".env")
    if not path.is_file():
        return
    if path.stat().st_mode & 0o077:
        print(f"warning: {path} is readable by others; run: chmod 600 {path}", file=sys.stderr)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        val = val.strip().strip("'\"")
        os.environ.setdefault(key, val)


KINDS = ("emby", "jellyfin")
SETTINGS_FILE = "settings.yaml"


@dataclass
class ServerConfig:
    kind: str  # "emby" | "jellyfin"
    url: str
    api_key: str
    library: str | None = None


def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(path: Path, required: bool = True) -> dict:
    """config.local.yaml (hand-edited, never rewritten) with the values saved from the web UI laid over it.
    The UI can set everything, so with required=False a missing file is fine: a fresh container starts empty."""
    cfg = load_yaml(path) if (required or path.exists()) else {}
    return apply_settings(cfg)


# ---- servers: one Emby and/or one Jellyfin ------------------------------------------------------------------------
def servers(cfg: dict) -> dict[str, dict]:
    """kind -> {'kind', 'url', 'library', 'enabled', ...}. Accepts `servers:` (several) or the older single `server:`."""
    out: dict[str, dict] = {}
    raw = cfg.get("servers")
    if isinstance(raw, dict):
        for kind, v in raw.items():
            if kind not in KINDS:
                raise ConfigError(f"servers.{kind}: only {' and '.join(KINDS)} are supported (one server per kind)")
            out[kind] = {**(v or {}), "kind": kind}
    elif isinstance(cfg.get("server"), dict) and cfg["server"]:
        kind = cfg["server"].get("kind", "emby")
        if kind not in KINDS:
            raise ConfigError(f"server.kind must be emby or jellyfin, got {kind!r}")
        out[kind] = {**cfg["server"], "kind": kind}
    for v in out.values():
        v.setdefault("enabled", True)
    return out


def enabled_servers(cfg: dict) -> list[str]:
    return [k for k, v in servers(cfg).items() if v.get("enabled", True)]


def server_key_env(cfg: dict, kind: str | None = None) -> str:
    s = servers(cfg)
    kind = kind or (next(iter(s), None) or "emby")
    return (s.get(kind) or {}).get("key_env") or KEY_ENV[kind]


def load_server(cfg: dict, kind: str | None = None) -> ServerConfig:
    s = servers(cfg)
    if kind is None:
        kinds = enabled_servers(cfg)
        if not kinds:
            raise ConfigError("no media server is configured: add the address and API key under Settings")
        kind = kinds[0]
    if kind not in s:
        raise ConfigError(f"no {kind} server is configured")
    sv = s[kind]
    if not sv.get("url"):
        raise ConfigError(f"the {kind} server address is not set (Settings, or servers.{kind}.url in the config file)")
    key = require_env(server_key_env(cfg, kind), f"the {kind} API key")
    return ServerConfig(kind=kind, url=str(sv["url"]).rstrip("/"), api_key=key, library=sv.get("library") or None)


def data_dir(cfg: dict) -> Path:
    """The shared data folder: settings, secrets, templates, fonts, the MDBList cache, overrides."""
    d = Path(os.environ.get("LAMINATOR_DATA") or cfg.get("data_dir") or ".")
    d.mkdir(parents=True, exist_ok=True)
    return d


def server_dir(cfg: dict, kind: str, root: Path | None = None) -> Path:
    """Everything that belongs to ONE server: state.db, originals/, out/, schedule.json. Never shared, so a restore
    on one server can never act on the other's items. `root` is the shared data folder (default: the config's)."""
    d = (root or data_dir(cfg)) / "servers" / kind
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---- settings saved from the web UI --------------------------------------------------------------------------------------
def load_settings(data: Path) -> dict:
    f = data / SETTINGS_FILE
    try:
        d = yaml.safe_load(f.read_text()) or {}
    except FileNotFoundError:
        return {}
    except yaml.YAMLError:
        return {}
    return d if isinstance(d, dict) else {}


def save_settings(data: Path, settings: dict) -> None:
    tmp = data / (SETTINGS_FILE + ".tmp")
    tmp.write_text("# written by the laminator web UI; overrides config.local.yaml for the keys it contains\n"
                   + yaml.safe_dump(settings, sort_keys=True, default_flow_style=False))
    tmp.replace(data / SETTINGS_FILE)


def reset_settings(data: Path, section: str) -> None:
    saved = load_settings(data)
    if saved.pop(section, None) is not None:
        save_settings(data, saved)


def apply_settings(cfg: dict, data: Path | None = None) -> dict:
    """Return cfg with the UI-saved settings laid over it (servers merged per kind; critic_rating/schedule replaced).
    `data` is the shared data folder; by default the one the config names."""
    saved = load_settings(data or data_dir(cfg))
    if not saved:
        return cfg
    out = copy.deepcopy(cfg)
    if isinstance(saved.get("servers"), dict):
        merged = servers(out)
        for kind, v in saved["servers"].items():
            if kind in KINDS and isinstance(v, dict):
                merged[kind] = {**merged.get(kind, {"kind": kind}), **v, "kind": kind}
        out["servers"] = merged
        out.pop("server", None)
    for key in ("critic_rating", "schedule", "cleanup", "backup_destination", "assets_dir"):
        if key in saved:
            out[key] = saved[key]
    return out


# ---- validating what the UI submits ------------------------------------------------------------------------------------
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".intranet", ".private")


def guess_scheme(rest: str) -> str:
    """The scheme for an address typed without one: http for an IP address, localhost, a single-word name or a local name
    (home.local, nas.lan); https for any other domain."""
    host = (urlparse("//" + rest).hostname or "").lower()
    try:
        ipaddress.ip_address(host)
        return "http"
    except ValueError:
        pass
    return "http" if "." not in host or host.endswith(LOCAL_SUFFIXES) else "https"


def normalize_url(raw: str) -> str:
    """http(s)://host[:port][/path] only: no credentials, query or fragment (those would leak into logs and links).
    An address typed without http:// or https:// gets one (see guess_scheme); one that has a scheme keeps it."""
    raw = raw.strip()
    if raw and "://" not in raw:
        raw = f"{guess_scheme(raw)}://{raw}"
    try:
        u = urlparse(raw)
        u.port  # a bad port raises here
    except ValueError:
        raise ValueError("that does not look like an address; try something like 192.168.1.10:8096") from None
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("the address must be a host name or IP address, with an optional http:// or https:// and port, like 192.168.1.10:8096")
    if u.username or u.password or u.query or u.fragment or len(raw) > 200:
        raise ValueError("the address must be just host:port (no username, query or #fragment)")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


_LIBRARY = re.compile(r"^[^\x00-\x1f]{0,100}$")


def validate_library(name: str) -> str | None:
    name = name.strip()
    if not _LIBRARY.match(name):
        raise ValueError("library name: up to 100 characters, no control characters")
    return name or None
