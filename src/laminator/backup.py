"""Backup and restore of laminator itself (not of your media server).

An archive (`laminator-backup-<time>.tar.gz`) holds, per server: a *consistent* snapshot of state.db (SQLite's own backup API,
never a raw copy of a live file), every saved original poster, the schedule record and any archive-*/ folders; plus settings.yaml,
overrides.yaml, your own templates and fonts. A manifest lists every file with its SHA-256.

API keys and the UI password hash are only included on request, and then only inside `secrets.enc` (AES-GCM, key from a
passphrase via scrypt). Posters are not secret, so the bulk of the archive stays plain and is streamed (never loaded into memory).

Restore verifies every hash and every member name BEFORE writing anything, refuses to overwrite existing data unless forced, and when
forced moves the old files into pre-restore-<time>/ instead of deleting them, so a restore can itself be undone."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from .config import KINDS, load_settings

FORMAT = 1
MANIFEST = "manifest.json"
SECRETS_MEMBER = "secrets.enc"
MAGIC = b"LMSEC1"
BACKUP_DIR = "backups"  # the default folder, inside the data folder
ENV_DIR = "LAMINATOR_BACKUP_DIR"  # or the folder is set with this environment variable (a folder saved in Settings wins)
AUTO_PREFIX = "laminator-backup-auto-"  # scheduled backups: the only ones retention ever deletes
NAME_RE = re.compile(r"^laminator-backup-[A-Za-z0-9._-]{1,60}\.tar\.gz$")
MIN_PASSPHRASE = 10
SECRET_FILES = ("secrets.env", "admin.hash")  # at the data root

_K = "|".join(KINDS)
ALLOWED = [re.compile(p) for p in (
    r"^manifest\.json$", r"^settings\.yaml$", r"^overrides\.yaml$", r"^secrets\.enc$",
    r"^templates/[A-Za-z0-9._-]+\.ya?ml$", r"^fonts/[A-Za-z0-9._ \[\],-]+\.(ttf|otf)$",
    r"^config/config\.local\.yaml$",
    rf"^servers/({_K})/(state\.db|schedule\.json)$",
    rf"^servers/({_K})/originals/[A-Za-z0-9._-]+$",
    rf"^servers/({_K})/archive-[0-9-]+/[A-Za-z0-9._/-]+$",
)]
MANAGED = ("servers", "settings.yaml", "overrides.yaml", "templates", "fonts")  # moved aside by a forced restore


class BackupError(Exception):
    pass


@dataclass
class Report:
    path: Path
    created: str = ""
    files: int = 0
    size: int = 0
    servers: dict = field(default_factory=dict)
    has_secrets: bool = False
    notes: list[str] = field(default_factory=list)


# ---- helpers ---------------------------------------------------------------------------------------------
def _safe_name(name: str) -> bool:
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return False
    if any(part in ("", ".", "..") for part in name.split("/")):
        return False
    return any(p.match(name) for p in ALLOWED)


class _Hashing(io.RawIOBase):
    """File-like wrapper that hashes what tarfile reads from it."""

    def __init__(self, f):
        self.f, self.h, self.n = f, hashlib.sha256(), 0

    def readable(self):
        return True

    def read(self, size=-1):
        b = self.f.read(size)
        self.h.update(b)
        self.n += len(b)
        return b

    readinto = None


def _add_file(tar: tarfile.TarFile, src: Path, name: str, files: dict, mode: int = 0o644, data: bytes | None = None) -> None:
    info = tarfile.TarInfo(name)
    info.mode, info.uid, info.gid, info.uname, info.gname = mode, 0, 0, "", ""
    if data is not None:
        info.size, info.mtime = len(data), int(time.time())
        tar.addfile(info, io.BytesIO(data))
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        return
    st = src.stat()
    info.size, info.mtime = st.st_size, int(st.st_mtime)
    with open(src, "rb") as f:
        hr = _Hashing(f)
        tar.addfile(info, hr)
    files[name] = {"sha256": hr.h.hexdigest(), "size": hr.n}
    if hr.n != st.st_size:
        raise BackupError(f"{src} changed while it was being backed up")


def _snapshot_db(db: Path, dest: Path) -> tuple[int, int]:
    """A consistent copy of a live SQLite file, with the stored original paths made relative (portable). -> (posters, metadata rows)."""
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        for item_id, path in out.execute("SELECT item_id, original_path FROM posters").fetchall():
            out.execute("UPDATE posters SET original_path=? WHERE item_id=?", ("originals/" + Path(path).name, item_id))
        out.commit()
        if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError(f"{db}: the database failed its integrity check; not backing up a damaged file")
        return (out.execute("SELECT count(*) FROM posters").fetchone()[0],
                out.execute("SELECT count(*) FROM meta_backups").fetchone()[0])
    finally:
        src.close()
        out.close()


# ---- secrets ---------------------------------------------------------------------------------------------
def _key(passphrase: str, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())


def encrypt_secrets(payload: dict, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"the passphrase must be at least {MIN_PASSPHRASE} characters")
    salt, nonce = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(nonce, json.dumps(payload).encode(), MAGIC)
    return MAGIC + salt + nonce + ct


def decrypt_secrets(blob: bytes, passphrase: str) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 28:
        raise BackupError("secrets.enc is not a laminator secrets block")
    salt, nonce, ct = blob[6:22], blob[22:34], blob[34:]
    try:
        return json.loads(AESGCM(_key(passphrase, salt)).decrypt(nonce, ct, MAGIC))
    except InvalidTag:
        raise BackupError("wrong passphrase, or the secrets block is damaged")


# ---- create ---------------------------------------------------------------------------------------------
def default_dir(root: Path) -> Path:
    return root / BACKUP_DIR


def configured_dir(root: Path) -> tuple[Path, str]:
    """Where the primary backups are kept, and why: a folder saved in Settings, else LAMINATOR_BACKUP_DIR, else <data>/backups."""
    v = load_settings(root).get("backup_dir")
    if isinstance(v, str) and v.startswith("/"):
        return Path(v), "settings"
    e = os.environ.get(ENV_DIR, "").strip()
    if e.startswith("/"):
        return Path(e), "environment"
    return default_dir(root), "default"


def validate_location(value: str, root: Path) -> str:
    """The Settings value for the backup folder: an absolute path without .. parts. It may not be the data folder itself or sit inside the
    parts of it that a restore replaces (servers, templates, fonts), or a restore would move the backups aside along with them."""
    v = (value or "").strip()
    if not v.startswith("/") or "\x00" in v or len(v) > 500 or any(x == ".." for x in v.split("/")):
        raise ValueError("the folder must be an absolute path (start with /), without .. parts; in a container it is the path of the mounted volume")
    v = v.rstrip("/") or "/"
    target, base = Path(v).resolve(), root.resolve()
    if target == base:
        raise ValueError("that is the data folder itself; use a folder of its own (or leave this blank for the default)")
    for sub in ("servers", "templates", "fonts", "cache"):
        if target == base / sub or (base / sub) in target.parents:
            raise ValueError(f"that is inside the data folder's {sub}/ folder, which a restore replaces; use another folder")
    return v


def check_writable(folder: Path) -> None:
    """Create the folder if needed and prove a file can be written there, with a plain sentence when not."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / f".laminator-write-test-{os.getpid()}-{time.time_ns()}"
        probe.write_bytes(b"ok")
        probe.unlink()
    except PermissionError:
        raise BackupError(f"{folder} is not writable by laminator. Check that the volume is mounted there and that its owner is the user "
                          "the container runs as.")
    except OSError as e:
        raise BackupError(f"{folder} cannot be used: {e.strerror or type(e).__name__}. Check that the volume is mounted there.")


def backups_dir(root: Path) -> Path:
    d, _ = configured_dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise BackupError(f"the backup folder {d} cannot be used: {e.strerror or type(e).__name__}. Check that the volume is mounted there.")
    return d


def location_info(root: Path) -> dict:
    """What the Settings and Status pages show: the folder, why it is that one, whether it works, and how many backups are in it."""
    d, source = configured_dir(root)
    info = {"path": str(d.resolve()), "source": source, "default": str(default_dir(root).resolve()), "custom": source != "default", "ok": True,
            "error": "", "count": 0, "stranded": 0}  # absolute for display: the data folder may be given as a relative path (the container's working folder)
    try:
        info["count"] = sum(1 for f in d.glob("laminator-backup-*.tar.gz") if f.is_file()) if d.is_dir() else 0
        if not d.exists():
            check_writable(d)
        elif not os.access(d, os.W_OK | os.X_OK):
            raise BackupError(f"{d} is not writable by laminator. Check that the volume is mounted there and that its owner is the user the container runs as.")
    except BackupError as e:
        info["ok"], info["error"] = False, str(e)
    dflt = default_dir(root)
    if source != "default" and dflt.is_dir():
        info["stranded"] = sum(1 for f in dflt.glob("laminator-backup-*.tar.gz") if f.is_file() and not f.is_symlink())
    return info


def move_backups(src: Path, dest: Path) -> list[str]:
    """Move the backups in `src` to `dest`. Each is copied to <name>.part, read back and compared (SHA-256), renamed into place, and only
    then removed from `src`; a backup that is already in `dest` with the same content is just removed from `src`. Anything that fails
    stops the move and leaves every not-yet-moved backup where it was. Returns the names moved."""
    if src.resolve() == dest.resolve():
        return []
    check_writable(dest)
    moved = []
    for f in sorted(src.glob("laminator-backup-*.tar.gz")):
        if not f.is_file() or f.is_symlink() or not NAME_RE.match(f.name):
            continue
        target = dest / f.name
        if target.exists():
            if target.stat().st_size == f.stat().st_size and _sha(target) == _sha(f):
                f.unlink()
                moved.append(f.name)
                continue
            raise BackupError(f"{target} already exists with different content; nothing was moved past {f.name}")
        part = dest / (f.name + ".part")
        try:
            with open(f, "rb") as fin, open(part, "wb") as fout:
                shutil.copyfileobj(fin, fout, 1 << 20)
                fout.flush()
                os.fsync(fout.fileno())
            if _sha(part) != _sha(f):
                raise BackupError(f"{f.name} did not copy cleanly; it was left where it was")
            os.chmod(part, 0o600)
            part.replace(target)
        except OSError as e:
            part.unlink(missing_ok=True)
            raise BackupError(f"could not copy {f.name} to {dest}: {e.strerror or type(e).__name__}; it was left where it was")
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        f.unlink()
        moved.append(f.name)
    return moved


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def create(root: Path, out: Path | None = None, include_secrets: bool = False, passphrase: str | None = None,
           config_file: Path | None = None) -> Report:
    """Write a backup of everything under `root`. The caller should hold the run lock so no job is mid-write."""
    if include_secrets and not passphrase:
        raise BackupError("including API keys and the password needs a passphrase to encrypt them")
    if include_secrets and len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"the passphrase must be at least {MIN_PASSPHRASE} characters")
    out = out or backups_dir(root) / time.strftime("laminator-backup-%Y%m%d-%H%M%S.tar.gz")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out.with_name(out.name + ".part")
    files: dict = {}
    servers: dict = {}
    try:
        with tempfile.TemporaryDirectory(prefix="laminator-bk-", dir=root) as stage, \
                tarfile.open(tmp_out, "w:gz") as tar:
            stage = Path(stage)
            sroot = root / "servers"
            for kind in KINDS:
                sd = sroot / kind
                if not sd.is_dir():
                    continue
                info = {"posters": 0, "meta": 0, "originals": 0}
                if (sd / "state.db").exists():
                    snap = stage / f"{kind}.db"
                    info["posters"], info["meta"] = _snapshot_db(sd / "state.db", snap)
                    _add_file(tar, snap, f"servers/{kind}/state.db", files)
                if (sd / "schedule.json").exists():
                    _add_file(tar, sd / "schedule.json", f"servers/{kind}/schedule.json", files)
                for f in sorted((sd / "originals").glob("*")) if (sd / "originals").is_dir() else []:
                    if f.is_file() and not f.is_symlink():
                        _add_file(tar, f, f"servers/{kind}/originals/{f.name}", files)
                        info["originals"] += 1
                for arc in sorted(sd.glob("archive-*")):
                    for f in sorted(arc.rglob("*")):
                        if f.is_file() and not f.is_symlink() and not f.name.endswith(("-wal", "-shm")):
                            rel = f.relative_to(sd).as_posix()
                            if _safe_name(f"servers/{kind}/{rel}"):
                                _add_file(tar, f, f"servers/{kind}/{rel}", files)
                servers[kind] = info
            for name in ("settings.yaml", "overrides.yaml"):
                if (root / name).exists():
                    _add_file(tar, root / name, name, files)
            for sub, pat in (("templates", "*.y*ml"), ("fonts", "*.[to]tf")):
                for f in sorted((root / sub).glob(pat)) if (root / sub).is_dir() else []:
                    if f.is_file() and _safe_name(f"{sub}/{f.name}"):
                        _add_file(tar, f, f"{sub}/{f.name}", files)
            if config_file and config_file.is_file():
                _add_file(tar, config_file, "config/config.local.yaml", files)
            if include_secrets:
                payload = {n: (root / n).read_text() for n in SECRET_FILES if (root / n).exists()}
                _add_file(tar, None, SECRETS_MEMBER, files, mode=0o600, data=encrypt_secrets(payload, passphrase))
            manifest = {"format": FORMAT, "laminator": __version__, "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "servers": servers, "has_secrets": include_secrets, "files": files}
            _add_file(tar, None, MANIFEST, {}, data=json.dumps(manifest, indent=1, sort_keys=True).encode())
        os.chmod(tmp_out, 0o600)  # it holds your state (and, if asked, encrypted keys): private by default
        tmp_out.replace(out)
    except BaseException:
        tmp_out.unlink(missing_ok=True)
        raise
    rep = Report(out, manifest["created"], len(files), out.stat().st_size, servers, include_secrets)
    if out.parent == backups_dir(root):  # so the Settings page does not have to unpack the new backup just to list it
        index = _load_index(root)
        index[out.name] = {**_fingerprint(out), "rep": {"created": rep.created, "files": rep.files, "servers": servers, "has_secrets": include_secrets}}
        _save_index(root, index)
    return rep


# ---- verify / inspect -----------------------------------------------------------------------------------------
def read_manifest(path: Path) -> dict:
    """Just the manifest (cheap enough for listing); does not verify the files."""
    try:
        with tarfile.open(path, "r:gz") as tar:
            f = tar.extractfile(tar.getmember(MANIFEST))
            m = json.loads(f.read(5_000_000))
    except (KeyError, tarfile.TarError, OSError, ValueError, EOFError) as e:
        raise BackupError(f"{path.name} is not a laminator backup ({type(e).__name__})")
    if m.get("format") != FORMAT:
        raise BackupError(f"{path.name}: unsupported backup format {m.get('format')!r} (this version reads format {FORMAT})")
    return m


def verify(path: Path, passphrase: str | None = None) -> Report:
    """Read the whole archive and check: every member name is allowed, only plain files, every SHA-256 matches the manifest,
    nothing is missing or extra. With a passphrase, also that the secrets block decrypts. Writes nothing."""
    m = read_manifest(path)
    want = m["files"]
    seen: set[str] = set()
    try:
        with tarfile.open(path, "r:gz") as tar:
            for member in tar:
                name = member.name
                if member.isdir():
                    raise BackupError(f"unexpected directory entry {name!r}")
                if not member.isreg():
                    raise BackupError(f"{name!r} is not a plain file (links and devices are refused)")
                if not _safe_name(name):
                    raise BackupError(f"member name {name!r} is not allowed in a laminator backup")
                if name == MANIFEST:
                    continue
                if name not in want:
                    raise BackupError(f"{name!r} is in the archive but not in its manifest")
                h, n = hashlib.sha256(), 0
                f = tar.extractfile(member)
                while chunk := f.read(1 << 20):
                    h.update(chunk)
                    n += len(chunk)
                if h.hexdigest() != want[name]["sha256"] or n != want[name]["size"]:
                    raise BackupError(f"{name}: checksum mismatch, the backup is damaged")
                seen.add(name)
    except (tarfile.TarError, OSError, EOFError) as e:
        raise BackupError(f"{path.name} could not be read: {type(e).__name__}: {e}")
    missing = set(want) - seen
    if missing:
        raise BackupError(f"{len(missing)} file(s) listed in the manifest are missing, e.g. {sorted(missing)[0]}")
    rep = Report(path, m["created"], len(want), path.stat().st_size, m.get("servers", {}), bool(m.get("has_secrets")))
    if rep.has_secrets and passphrase:
        decrypt_secrets(_read_member(path, SECRETS_MEMBER), passphrase)
        rep.notes.append("the secrets block decrypts with that passphrase")
    elif rep.has_secrets:
        rep.notes.append("contains encrypted API keys / password (give the passphrase to check or restore them)")
    return rep


def _read_member(path: Path, name: str) -> bytes:
    with tarfile.open(path, "r:gz") as tar:
        return tar.extractfile(tar.getmember(name)).read()


def _index_path(root: Path) -> Path:
    return root / "cache" / "backup-index.json"


def _load_index(root: Path) -> dict:
    try:
        data = json.loads(_index_path(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_index(root: Path, index: dict) -> None:
    """Best effort: the index only saves time, so a failed write is ignored (the next listing rebuilds it)."""
    try:
        p = _index_path(root)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(index))
        tmp.replace(p)
    except OSError:
        pass


def _fingerprint(f: Path) -> dict:
    st = f.stat()
    return {"size": st.st_size, "mtime": st.st_mtime_ns}


def list_backups(root: Path) -> list[Report]:
    """Every backup with the summary from its manifest. The manifest is the last member of the archive, so reading it means unpacking
    the whole file (about 10 s for a 2 GB backup). The summaries are therefore remembered in cache/backup-index.json, keyed by the file's
    name, size and modification time; a backup that changed or is new is read once."""
    out, index, changed = [], _load_index(root), False
    try:
        files = sorted(backups_dir(root).glob("laminator-backup-*.tar.gz"), reverse=True)
    except (BackupError, OSError):
        return []  # the folder is missing or not writable: the Settings page says so (location_info); there is nothing to list
    for f in files:
        fp = _fingerprint(f)
        hit = index.get(f.name)
        if hit and hit.get("size") == fp["size"] and hit.get("mtime") == fp["mtime"] and isinstance(hit.get("rep"), dict):
            r = hit["rep"]
            out.append(Report(f, r["created"], r["files"], fp["size"], r["servers"], r["has_secrets"]))
            continue
        try:
            m = read_manifest(f)
            rep = Report(f, m["created"], len(m["files"]), fp["size"], m.get("servers", {}), bool(m.get("has_secrets")))
            index[f.name] = {**fp, "rep": {"created": rep.created, "files": rep.files, "servers": rep.servers, "has_secrets": rep.has_secrets}}
            changed = True
            out.append(rep)
        except BackupError as e:
            out.append(Report(f, "", 0, fp["size"], {}, False, [f"unreadable: {e}"]))
    gone = set(index) - {f.name for f in files}
    for name in gone:
        del index[name]
    if changed or gone:
        _save_index(root, index)
    return out


# ---- restore -----------------------------------------------------------------------------------------------------
def _existing(root: Path, restoring_secrets: bool) -> list[str]:
    found = []
    for n in MANAGED + (SECRET_FILES if restoring_secrets else ()):
        p = root / n
        if p.is_file() or (p.is_dir() and any(f.is_file() for f in p.rglob("*"))):  # empty folders do not count
            found.append(n)
    return found


def restore(path: Path, root: Path, force: bool = False, dry_run: bool = False, passphrase: str | None = None) -> Report:
    """Verify, then put the backup into `root`. The caller should hold the run lock."""
    rep = verify(path, passphrase)
    restoring_secrets = rep.has_secrets and bool(passphrase)
    if rep.has_secrets and not passphrase:
        rep.notes.append("API keys and the password were NOT restored (no passphrase given)")
    clash = _existing(root, restoring_secrets)
    if clash and not force:
        raise BackupError(f"{root} already has {', '.join(clash)}. Restoring replaces them; the old copies are moved aside, "
                          "not deleted, but you have to say so explicitly (--force).")
    if dry_run:
        rep.notes.append("dry run: nothing was written" + (f"; would move aside: {', '.join(clash)}" if clash else ""))
        return rep
    root.mkdir(parents=True, exist_ok=True)
    root_r = root.resolve()
    aside = None
    if clash:
        aside = root / time.strftime("pre-restore-%Y%m%d-%H%M%S")
        aside.mkdir()
        for n in clash:
            (root / n).rename(aside / n)
        rep.notes.append(f"previous files moved to {aside.name}/")
    with tarfile.open(path, "r:gz") as tar:
        for member in tar:
            name = member.name
            if name in (MANIFEST, SECRETS_MEMBER):
                continue
            if name == "config/config.local.yaml":
                dest = root / "restored-config.local.yaml"  # never overwrite the live config file
                rep.notes.append("config.local.yaml saved as restored-config.local.yaml (compare, then copy it into place yourself)")
            else:
                dest = root / name
            if not dest.resolve().is_relative_to(root_r):
                raise BackupError(f"refusing to write outside {root}: {name}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as out:
                shutil.copyfileobj(tar.extractfile(member), out)
            os.utime(dest, (member.mtime, member.mtime))  # poster history is ordered by file age: keep it
    for kind in rep.servers:
        db = root / "servers" / kind / "state.db"
        if db.exists():
            con = sqlite3.connect(db)
            try:
                for item_id, p in con.execute("SELECT item_id, original_path FROM posters").fetchall():
                    new = (root / "servers" / kind / "originals" / Path(p).name).resolve()
                    con.execute("UPDATE posters SET original_path=? WHERE item_id=?", (str(new), item_id))
                con.commit()
            finally:
                con.close()
    if restoring_secrets:
        for fname, text in decrypt_secrets(_read_member(path, SECRETS_MEMBER), passphrase).items():
            if fname in SECRET_FILES:
                fd = os.open(root / fname, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write(text)
        rep.notes.append("API keys and the password were restored")
    return rep


def create_auto(root: Path, keep: int = 5, config_file: Path | None = None) -> tuple[Report, list[str]]:
    """A scheduled backup: written, then VERIFIED, and only then are older scheduled backups beyond `keep` removed. If the new one does
    not verify it is deleted and every older backup is left alone. Hand-made and uploaded backups are never touched. No secrets."""
    if not 1 <= keep <= 50:
        raise BackupError("keep must be between 1 and 50")
    out = backups_dir(root) / time.strftime(AUTO_PREFIX + "%Y%m%d-%H%M%S.tar.gz")
    rep = create(root, out, False, None, config_file)
    try:
        verify(rep.path)
    except BackupError:
        rep.path.unlink(missing_ok=True)
        raise
    autos = sorted((f for f in backups_dir(root).glob(AUTO_PREFIX + "*.tar.gz") if f.is_file() and not f.is_symlink() and NAME_RE.match(f.name)),
                   key=lambda p: p.name, reverse=True)
    pruned = []
    for old in autos[keep:]:
        old.unlink(missing_ok=True)
        pruned.append(old.name)
    return rep, pruned
