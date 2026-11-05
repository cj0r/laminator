"""Where backups are copied to, besides <data>/backups: a local path (a Docker bind mount, a mounted NAS or cloud folder) or an SMB share.

Every copy is written as <name>.part, read back and compared (SHA-256) with the local file, and only then renamed into place, so a half-copied
file never looks like a backup. Only scheduled backups (laminator-backup-auto-*) are ever deleted at a destination; nothing else on it is touched.
Errors are turned into plain sentences and never contain the password. Cloud drives (Google Drive, OneDrive, ...) are deliberately not built in:
mount one as a folder (for example with rclone) and use the local-path option."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import socket
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .backup import AUTO_PREFIX, NAME_RE

SMB_PASSWORD_ENV = "LAMINATOR_SMB_PASSWORD"
BAD_NETWORK_NAME, ACCESS_DENIED, LOGON_DENIED_SHARE = 0xC00000CC, 0xC0000022, 0xC000006D
NOT_FOUND = (0xC0000034, 0xC000003A, 0xC0000033)  # object name / path not found, name invalid
TYPES = ("none", "local", "smb")
_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")
_SHARE = re.compile(r"^[A-Za-z0-9._$ -]{1,80}$")
_PART = re.compile(r"^[A-Za-z0-9._ -]{1,80}$")
_USER = re.compile(r"^[A-Za-z0-9._@ -]{1,100}$")
_DOMAIN = re.compile(r"^[A-Za-z0-9._-]{1,63}$")


class DestError(Exception):
    pass


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


# ---- validating what the settings form submits -------------------------------------------------------------------------
def validate(form: dict) -> dict:
    """Clean `backup_destination` settings from untrusted form values. Raises ValueError with a sentence for the user."""
    t = (form.get("type") or "none").strip()
    if t not in TYPES:
        raise ValueError("unknown destination type")
    if t == "none":
        return {"type": "none"}
    if t == "local":
        p = (form.get("path") or "").strip()
        if not p.startswith("/") or "\x00" in p or len(p) > 500 or any(x == ".." for x in p.split("/")):
            raise ValueError("the path must be absolute (start with /), without .. parts; in a container it is the path of the mounted volume")
        return {"type": "local", "path": p.rstrip("/") or "/"}
    host = (form.get("host") or "").strip()
    if not _HOST.match(host):
        raise ValueError("the server must be a host name or IP address, like nas.local or 192.168.1.20")
    try:
        port = int(form.get("port") or 445)
    except ValueError:
        raise ValueError("the port must be a number")
    if not 1 <= port <= 65535:
        raise ValueError("the port must be between 1 and 65535")
    share = (form.get("share") or "").strip()
    if not _SHARE.match(share):
        raise ValueError("the share name may use letters, digits and . _ $ - and spaces only (no slashes)")
    folder = (form.get("folder") or "").strip().strip("/\\")
    parts = [x for x in re.split(r"[\\/]+", folder) if x]
    if len(parts) > 5 or any(not _PART.match(x) or x in (".", "..") for x in parts):
        raise ValueError("the folder must be a short path of plain names, like laminator/backups (no .. and at most 5 levels)")
    user = (form.get("username") or "").strip()
    if not _USER.match(user):
        raise ValueError("enter the SMB user name")
    domain = (form.get("domain") or "").strip()
    if domain and not _DOMAIN.match(domain):
        raise ValueError("the domain may use letters, digits and . _ - only")
    out = {"type": "smb", "host": host, "port": port, "share": share, "folder": "/".join(parts), "username": user}
    if domain:
        out["domain"] = domain
    return out


# ---- the two kinds of destination -------------------------------------------------------------------------------------------
class Dest:
    kind = "?"

    def describe(self) -> str:
        raise NotImplementedError

    def check(self) -> str:
        """Prove the destination works (connect, write, read back, delete a small test file). Returns a sentence; raises DestError."""
        raise NotImplementedError

    def put(self, src: Path, name: str) -> None:
        raise NotImplementedError

    def names(self) -> list[str]:
        raise NotImplementedError

    def delete(self, name: str) -> None:
        raise NotImplementedError

    def prune_auto(self, keep: int) -> list[str]:
        """Delete scheduled backups beyond the `keep` newest; any other file at the destination is left alone."""
        autos = sorted((n for n in self.names() if n.startswith(AUTO_PREFIX) and NAME_RE.match(n)), reverse=True)
        gone = autos[keep:]
        for n in gone:
            self.delete(n)
        return gone


@dataclass
class LocalDest(Dest):
    path: Path
    kind = "local"

    def describe(self) -> str:
        return f"folder {self.path}"

    def _dir(self) -> Path:
        d = self.path
        if not d.is_dir():
            raise DestError(f"{d} does not exist or is not a folder (in a container: is the volume mounted?)")
        return d

    def check(self) -> str:
        d = self._dir()
        probe = d / f".laminator-test-{secrets.token_hex(4)}"
        try:
            probe.write_bytes(b"laminator")
            ok = probe.read_bytes() == b"laminator"
            probe.unlink()
        except OSError as e:
            raise DestError(f"cannot write to {d}: {e.strerror or type(e).__name__}")
        if not ok:
            raise DestError(f"{d} did not return what was written")
        free = os.statvfs(d)
        return f"{d} is writable ({free.f_bavail * free.f_frsize / 1e9:.1f} GB free)"

    def put(self, src: Path, name: str) -> None:
        d = self._dir()
        part, final = d / (name + ".part"), d / name
        try:
            with open(src, "rb") as a, open(part, "wb") as b:
                while chunk := a.read(1 << 20):
                    b.write(chunk)
                b.flush()
                os.fsync(b.fileno())
            if _sha_file(part) != _sha_file(src):
                raise DestError("the copy did not match the original when read back; not kept")
            part.replace(final)
        except OSError as e:
            raise DestError(f"could not write to {d}: {e.strerror or type(e).__name__}")
        finally:
            part.unlink(missing_ok=True)

    def names(self) -> list[str]:
        d = self._dir()
        return [f.name for f in d.glob("laminator-backup-*.tar.gz") if f.is_file() and not f.is_symlink() and NAME_RE.match(f.name)]

    def delete(self, name: str) -> None:
        f = self._dir() / name
        if NAME_RE.match(name) and f.is_file() and not f.is_symlink():
            f.unlink()


@dataclass
class SmbDest(Dest):
    host: str
    share: str
    folder: str
    username: str
    password: str
    port: int = 445
    domain: str = ""
    kind = "smb"

    def describe(self) -> str:
        return f"SMB share \\\\{self.host}\\{self.share}" + (f"\\{self.folder.replace('/', chr(92))}" if self.folder else "")

    # -- plumbing: every call goes through here so errors read well and the password never leaks ------------------------
    def _unc(self, name: str = "") -> str:
        parts = [self.host, self.share] + ([x for x in self.folder.split("/") if x]) + ([name] if name else [])
        return "\\\\" + "\\".join(parts)

    def _session(self):
        import smbclient
        user = f"{self.domain}\\{self.username}" if self.domain else self.username
        smbclient.register_session(self.host, username=user, password=self.password, port=self.port, connection_timeout=15, encrypt=False)
        return smbclient

    def _wrap(self, fn):
        import smbclient
        from smbprotocol import exceptions as E
        try:
            return fn(self._session())
        except DestError:
            raise
        except E.LogonFailure:
            raise DestError("the SMB server rejected the user name or password")
        except E.SMBAuthenticationError as e:
            raise DestError(f"SMB authentication failed ({type(e).__name__})")
        except E.SMBOSError as e:  # carries an NT status code
            st = (e.ntstatus or 0) & 0xFFFFFFFF
            if st == BAD_NETWORK_NAME:
                raise DestError(f"the share {self.share!r} was not found on {self.host}")
            if st in (ACCESS_DENIED, LOGON_DENIED_SHARE):
                raise DestError("no permission to write to that share or folder")
            if st in NOT_FOUND:
                raise DestError(f"the folder {self.folder!r} does not exist on the share")
            raise DestError(f"SMB error 0x{st:08X}")
        except E.SMBException as e:
            raise DestError(f"SMB error ({type(e).__name__})")
        except (socket.timeout, TimeoutError):
            raise DestError(f"{self.host}:{self.port} did not answer in time")
        except ValueError as e:  # smbprotocol reports every connection failure (refused, unreachable, unknown host) this way
            if str(e).startswith("Failed to connect"):
                why = str(e).rsplit(": ", 1)[-1][:120]
                raise DestError(f"could not reach {self.host}:{self.port} ({why})")
            raise DestError(f"SMB error ({type(e).__name__})")
        except OSError as e:
            raise DestError(f"could not reach {self.host}:{self.port} ({e.strerror or type(e).__name__})")
        finally:
            try:
                smbclient.delete_session(self.host, port=self.port)
            except Exception:
                pass

    def check(self) -> str:
        probe = f".laminator-test-{secrets.token_hex(4)}"

        def go(c):
            c.makedirs(self._unc(), exist_ok=True, port=self.port)
            with c.open_file(self._unc(probe), mode="wb", port=self.port) as f:
                f.write(b"laminator")
            with c.open_file(self._unc(probe), mode="rb", port=self.port) as f:
                ok = f.read() == b"laminator"
            c.remove(self._unc(probe), port=self.port)
            if not ok:
                raise DestError("the share did not return what was written")
        self._wrap(go)
        return f"connected to {self.describe()} and wrote a test file there"

    def put(self, src: Path, name: str) -> None:
        part_name = name + ".part"

        def go(c):
            c.makedirs(self._unc(), exist_ok=True, port=self.port)
            try:
                h = hashlib.sha256()
                with open(src, "rb") as a, c.open_file(self._unc(part_name), mode="wb", port=self.port) as b:
                    while chunk := a.read(1 << 20):
                        b.write(chunk)
                h2 = hashlib.sha256()
                with c.open_file(self._unc(part_name), mode="rb", port=self.port) as b:
                    while chunk := b.read(1 << 20):
                        h2.update(chunk)
                if h2.hexdigest() != _sha_file(src):
                    raise DestError("the copy did not match the original when read back; not kept")
                try:
                    c.remove(self._unc(name), port=self.port)  # an older file of the same name is replaced
                except Exception:
                    pass
                c.rename(self._unc(part_name), self._unc(name), port=self.port)
            finally:
                try:
                    c.remove(self._unc(part_name), port=self.port)
                except Exception:
                    pass
        self._wrap(go)

    def names(self) -> list[str]:
        from smbprotocol import exceptions as E

        def go(c):
            try:
                return [n for n in c.listdir(self._unc(), port=self.port) if NAME_RE.match(n)]
            except E.SMBOSError as e:
                if (e.ntstatus or 0) & 0xFFFFFFFF in NOT_FOUND:
                    return []  # the folder is created on the first copy
                raise
        return self._wrap(go)

    def delete(self, name: str) -> None:
        if NAME_RE.match(name):
            self._wrap(lambda c: c.remove(self._unc(name), port=self.port))


def from_cfg(cfg: dict, env=None) -> Dest | None:
    """The configured destination, or None. The SMB password comes from the environment / saved Settings, never the config file."""
    env = os.environ if env is None else env
    d = cfg.get("backup_destination")
    if not isinstance(d, dict) or d.get("type") in (None, "none"):
        return None
    try:
        c = validate(d)
    except ValueError:
        return None  # a damaged setting must not take backups down: they still go to <data>/backups
    if c["type"] == "local":
        return LocalDest(Path(c["path"]))
    pw = env.get(SMB_PASSWORD_ENV, "")
    return SmbDest(c["host"], c["share"], c["folder"], c["username"], pw, c["port"], c.get("domain", ""))


def push(path: Path, dest: Dest, keep: int | None = None) -> list[str]:
    """Copy one backup to the destination (verified), then (for a scheduled backup) prune older scheduled ones there. Returns pruned names."""
    dest.put(path, path.name)
    return dest.prune_auto(keep) if keep and path.name.startswith(AUTO_PREFIX) else []
