"""API keys saved from the web UI. A KEY=VALUE file in the data folder, mode 600, loaded into the environment
**above** .env and real environment variables: a key saved in Settings overrides them, so keys can be changed from the
UI without editing files. The environment's own value is remembered underneath, so removing the saved key restores
it exactly. Plain text on disk, like .env: protect the data folder (backups included). Values are never logged or echoed."""
from __future__ import annotations

import os
import re
from pathlib import Path

FILE = "secrets.env"
VALID_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,60}$")
VALID_VALUE = re.compile(r"^[A-Za-z0-9._~+/=-]{8,200}$")  # what real API keys look like; nothing that could break the file
# passwords (the SMB one) can use any printable character: only control characters (a newline would break the file) are refused
LOOSE_NAMES = {"LAMINATOR_SMB_PASSWORD"}
VALID_LOOSE = re.compile(r"^[^\x00-\x1f\x7f]{1,128}$")


def _value_ok(name: str, value: str) -> bool:
    return bool((VALID_LOOSE if name in LOOSE_NAMES else VALID_VALUE).match(value))


# name -> the value the environment had before a saved key overrode it (None = it had none)
_UNDER: dict[str, str | None] = {}


def _override(name: str, value: str) -> None:
    _UNDER.setdefault(name, os.environ.get(name))
    os.environ[name] = value


def path(data: Path) -> Path:
    return data / FILE


def read(data: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        lines = path(data).read_text().splitlines()
    except FileNotFoundError:
        return out
    for line in lines:
        k, sep, v = line.partition("=")
        k = k.strip()
        v = v if k in LOOSE_NAMES else v.strip()  # a password's own leading/trailing spaces are part of it
        if sep and VALID_NAME.match(k) and _value_ok(k, v):
            out[k] = v
    return out


def _write(data: Path, values: dict[str, str]) -> None:
    f = path(data)
    tmp = f.with_name(FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # created private: never world-readable, even briefly
    with os.fdopen(fd, "w") as fh:
        fh.write("# written by the laminator web UI; same care as .env\n")
        for k, v in sorted(values.items()):
            fh.write(f"{k}={v}\n")
    os.chmod(tmp, 0o600)
    tmp.replace(f)


def save(data: Path, name: str, value: str) -> None:
    if not VALID_NAME.match(name):
        raise ValueError("bad variable name")
    if not _value_ok(name, value):
        raise ValueError("that does not look like a password (1-128 characters, no control characters)" if name in LOOSE_NAMES
                         else "that does not look like an API key (8-200 characters: letters, digits and . _ ~ + / = -)")
    values = read(data)
    values[name] = value
    _write(data, values)
    _override(name, value)


def clear(data: Path, name: str) -> None:
    """Remove the saved key; whatever .env/the environment provided before it comes back."""
    values = read(data)
    if values.pop(name, None) is None:
        return
    _write(data, values)
    under = _UNDER.pop(name, None)
    if under is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = under


def load_into_env(data: Path) -> None:
    """Called at startup after .env: saved keys override what the environment provides (and remember it)."""
    for k, v in read(data).items():
        _override(k, v)


def source(data: Path, name: str) -> str:
    """'settings' (saved from the UI), 'environment' (real environment or .env) or 'missing'."""
    cur = os.environ.get(name)
    if not cur:
        return "missing"
    return "settings" if read(data).get(name) == cur else "environment"


def overrides_environment(data: Path, name: str) -> bool:
    """True when a saved key is hiding a different value from .env / the environment."""
    return source(data, name) == "settings" and _UNDER.get(name) not in (None, os.environ.get(name))


def mask(value: str | None, name: str | None = None) -> str:
    """Never the whole key: bullets, plus the last four characters when the key is long enough for that to be safe.
    A password gets bullets only."""
    if not value:
        return ""
    return "•" * 8 + (value[-4:] if len(value) >= 16 and name not in LOOSE_NAMES else "")
