"""The poster pipeline as functions that yield Events. Nothing here prints or reads sys.argv, so the
CLI and a web UI can both drive it and render the same results."""
from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from PIL import Image

from . import meta as metamod
from . import migrate, schedule
from .config import MDBLIST_ENV, ConfigError, ServerConfig, data_dir, enabled_servers, load_server, require_env, server_dir, servers
from .assets import load_template
from .mdblist import MDBList, pick_score
from . import baseposters as bp
from .render import RENDER_VERSION, is_overlaid, render
from .server import MediaServer, connect
from .store import Store


# ---- events ---------------------------------------------------------------------------------
@dataclass
class Event:
    kind: str  # render apply same skip lost new critic restore clean info warn error
    name: str = ""
    message: str = ""
    item_id: str = ""

    @property
    def problem(self) -> bool:
        return self.kind in ("warn", "error")

    def line(self) -> str:
        label = {"warn": "WARN", "error": "ERROR"}.get(self.kind, self.kind)
        if self.kind == "info":
            return self.message
        if not self.name:
            return f"{label:<6} {self.message}"
        head = f"{label:<6} {self.name}"
        return f"{head}: {self.message}" if self.message else head


class Busy(Exception):
    pass


@contextmanager
def run_lock(data: Path):
    """Only one writing run at a time per data folder (scheduled and manual runs must not overlap)."""
    f = open(data / ".lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise Busy("another laminator run is already using this data folder")
    try:
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


# ---- workspace ----------------------------------------------------------------------------------
@dataclass
class Workspace:
    cfg: dict
    scfg: ServerConfig
    srv: MediaServer
    store: Store
    mdb: MDBList | None
    data: Path  # THIS server's folder: state.db, originals/, out/, schedule.json (never shared with another server)
    root: Path | None = None  # the shared data folder: templates, fonts, the MDBList cache, overrides, the run lock

    def __post_init__(self):
        if self.root is None:
            self.root = self.data

    @property
    def assets(self) -> Path:
        """Where custom base posters live (Settings > Custom posters; default <data>/assets)."""
        return bp.assets_root(self.cfg, self.root)

    @property
    def originals(self) -> Path:
        d = (self.data / "originals").resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def out(self) -> Path:
        d = (self.data / "out").resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d


def legacy_kind(cfg: dict) -> str | None:
    """Which server owned the old single-server state files in the data root: Emby if configured, else the first."""
    s = servers(cfg)
    return "emby" if "emby" in s else next(iter(s), None)


def open_workspace(cfg: dict, library: str | None = None, need_mdblist: bool = True, kind: str | None = None) -> Workspace:
    scfg = load_server(cfg, kind)
    if library:
        scfg.library = library
    root, sdir = data_dir(cfg), server_dir(cfg, scfg.kind)
    if scfg.kind == legacy_kind(cfg):
        note = migrate.migrate_legacy(root, sdir)  # one-time, safe, verified (see migrate.py)
        if note:
            print(f"note: {note}", file=sys.stderr)
    mdb = None
    if need_mdblist:
        mdb = MDBList(require_env(MDBLIST_ENV, "the MDBList API key"), root / "cache/mdblist")
    return Workspace(cfg, scfg, connect(scfg), Store(sdir / "state.db"), mdb, sdir, root)


def require_scope(item, library, all_, configured_library=None) -> None:
    """Writing actions must be scoped on purpose: a library, an item, or an explicit all."""
    if not (item or library or configured_library or all_):
        raise ConfigError(
            "refusing to touch every item on the server by accident. "
            "Pass --library NAME, --item NAME, or --all if you really mean everything."
        )


# ---- helpers ----------------------------------------------------------------------------------------
class SetupError(Exception):
    """Something laminator needs has not been set up yet (shown to the user as a plain sentence, not a Python error)."""


class Unsure(Exception):
    pass


OVERLAID = ("the poster on the server already carries a laminator overlay, but {why}. Not used as an original, so nothing is stacked. "
            "Restore a laminator backup that has the originals, or replace the poster in the server, then run again")


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:60]


def _ctype(data: bytes) -> str:
    fmt = Image.open(io.BytesIO(data)).format or "JPEG"
    return Image.MIME.get(fmt, "image/jpeg")


def _short(v, n=60):
    v = metamod.show_value(v).replace("\n", " ")
    return v if len(v) <= n else v[: n - 1] + "…"


ZERO_BADGES = ("0", "0.0", "0.00")  # what a score of 0 drew before a 0 from MDBList was treated as "no score"


def _matches(item_filter, name, item_id) -> bool:
    return not item_filter or item_filter.lower() == name.lower() or item_filter == item_id


def all_libraries(srv) -> dict:
    out = {l["Name"]: srv.items(l["Name"]) for l in srv.libraries() if l.get("CollectionType") in ("movies", "tvshows")}
    for name, items in out.items():
        for it in items:
            try:
                it.library = name  # custom posters can be kept per library
            except AttributeError:
                pass
    return out


def poster_context(ws: Workspace, overrides_path: Path, required: bool) -> tuple[list, dict, list[Event], dict]:
    """(items in scope, {item id: score override}, warnings, {item id: per-title settings}). All libraries are loaded so a title that
    exists in two of them is caught as ambiguous."""
    entries = metamod.for_server(metamod.load_overrides(overrides_path, required=required), ws.scfg.kind)
    libs = all_libraries(ws.srv)
    absent: list | None = [] if len(enabled_servers(ws.cfg)) > 1 else None
    per, errors = metamod.per_item_settings(entries, libs, absent)
    overrides = {i: v["score"] for i, v in per.items() if "score" in v}
    events = [Event("warn", message=f"override ignored: {e}") for e in errors]
    if absent:   # with Emby and Jellyfin side by side the servers need not hold the same titles
        events.append(Event("info", message=f"{len(absent)} override(s) are for titles that are not on this server; nothing to do here for them"))
    lib = ws.scfg.library
    if lib:
        names = {n.lower(): n for n in libs}
        if lib.lower() not in names:
            raise ConfigError(f"library {lib!r} not found; available: {', '.join(libs)}")
        items = libs[names[lib.lower()]]
    else:
        items = [it for v in libs.values() for it in v]
    return items, overrides, events, per


def poster_history(orig_dir: Path, item_id: str, current_sha: str | None = None) -> list[dict]:
    """Every distinct poster saved for an item, newest first: its own <id>.img and <id>-<hash>.img files (a repeated picture counts
    once, under its earliest file). Marks the first (the poster from before laminator touched the item) and the current original."""
    pat = re.compile(rf"^{re.escape(item_id)}(-[0-9a-f]{{8}})?\.img$")
    seen: dict[str, dict] = {}
    for f in sorted((f for f in orig_dir.glob(f"{item_id}*.img") if pat.match(f.name) and f.is_file() and not f.is_symlink()), key=lambda p: (p.stat().st_mtime, p.name != f"{item_id}.img", p.name)):  # the plain <id>.img is always the first ever saved
        raw = f.read_bytes()
        try:
            Image.open(io.BytesIO(raw)).verify()  # only real pictures are offered back to the user
        except Exception:
            continue
        h = sha(raw)
        if h not in seen:
            seen[h] = {"name": f.name, "path": f, "sha": h, "mtime": f.stat().st_mtime, "size": f.stat().st_size}
    out = list(seen.values())
    for i, e in enumerate(out):
        e["first"], e["current"] = i == 0, e["sha"] == current_sha
    return out[::-1]


def original_file(row, orig_dir: Path) -> Path:
    """The saved original for a state row. Older rows stored a relative path; if it does not resolve, look in this
    server's originals folder by file name (that is where a moved or migrated file lives)."""
    p = Path(row["original_path"])
    if p.exists():
        return p
    alt = orig_dir / p.name
    return alt if alt.exists() else p


def get_original(srv, store, orig_dir: Path, it, asset: Path | None = None) -> tuple:
    """(state row, original bytes, events) for an item, reconciling with what the server holds now.

    The state DB records what we believe the server holds (image tag + pixel hash). When the tag differs,
    download the current poster once and classify it:
      - same hash as recorded        -> only the tag changed; nothing to do
      - same as our saved original   -> the overlay was lost (e.g. "Replace all images"); mark for re-apply
      - anything else                -> the user picked a new poster in the media server: adopt it as the
                                        new original (the old file is kept) and render on that
    Items already in the DB are otherwise never downloaded: the server may be serving our overlay.

    `asset` is a custom base poster found for the item (see baseposters.py). It wins over the server's poster: it becomes the
    original (the poster the server had stays in the history), and an asset that changes is picked up the next time.
    """
    events: list[Event] = []
    row = store.get(it.id)
    custom = None
    if asset is not None:
        try:
            custom = bp.load(asset)
        except bp.AssetError as e:
            raise Unsure(f"custom poster {asset.name}: {e}")
    if row is None and custom is not None:
        data = srv.download_primary(it.id)[0] if getattr(it, "has_poster", True) else None  # a title with no poster in the server has only the custom one
        if data is not None and not is_overlaid(data):  # keep the server's own poster as the first entry of the history (a way back)
            (orig_dir / f"{it.id}.img").write_bytes(data)
            store.put_original(it.id, it.name, str(orig_dir / f"{it.id}.img"), sha(data), it.poster_tag)
            row = store.get(it.id)
        else:  # no poster in the server, or a fresh data folder meeting our own overlay: the custom poster is the only safe base
            (orig_dir / f"{it.id}.img").write_bytes(custom)
            store.put_original(it.id, it.name, str(orig_dir / f"{it.id}.img"), sha(custom), it.poster_tag)
            row = store.get(it.id)
    if row is None:
        data = srv.download_primary(it.id)[0]
        if is_overlaid(data):
            raise Unsure(OVERLAID.format(why="there is no saved original for it in this data folder (a fresh or wrong data folder?)"))
        op = orig_dir / f"{it.id}.img"
        op.write_bytes(data)
        store.put_original(it.id, it.name, str(op), sha(data), it.poster_tag)
        row = store.get(it.id)
    elif it.poster_tag != row["served_tag"] or row["served_sha"] is None:
        cur = srv.download_primary(it.id)[0]
        csha = sha(cur)
        if row["served_sha"] is None and it.poster_tag == row["served_tag"]:
            store.set_served(it.id, it.poster_tag, csha)  # backfill from before hashes were recorded
        elif row["served_sha"] and csha == row["served_sha"]:
            store.set_served(it.id, it.poster_tag, csha)
        elif csha == row["original_sha"]:
            if row["applied_hash"] is not None:
                events.append(Event("lost", it.name, "overlay is gone from the server (original poster is back); will re-apply", it.id))
            store.set_served(it.id, it.poster_tag, csha, applied_hash=None)
        elif row["served_sha"] is None:
            raise Unsure("cannot tell whether the server poster is ours or a new one; run `restore` first")
        else:
            same = next((h for h in poster_history(orig_dir, it.id) if h["sha"] == csha), None)  # a picture we already hold
            if not same and is_overlaid(cur):
                raise Unsure(OVERLAID.format(why="it does not match any poster saved for this item"))
            if same:
                new = same["path"]
            else:
                new = orig_dir / f"{it.id}-{csha[:8]}.img"
                new.write_bytes(cur)
            events.append(Event("new", it.name, "poster was changed on the server; using it as the new original "
                                f"(previous original kept at {row['original_path']})", it.id))
            store.set_original(it.id, str(new), csha, it.poster_tag)
        row = store.get(it.id)
    if custom is not None and row["original_sha"] != sha(custom):
        csha_ = sha(custom)
        same = next((h for h in poster_history(orig_dir, it.id) if h["sha"] == csha_), None)
        if same:
            new_path = same["path"]
        else:
            new_path = orig_dir / f"{it.id}-{csha_[:8]}.img"
            new_path.write_bytes(custom)
        events.append(Event("new", it.name, f"custom poster {asset.name} is now the base poster "
                            "(the previous one stays in the poster history)", it.id))
        store.set_original(it.id, str(new_path), csha_, it.poster_tag)
        row = store.get(it.id)
    op = original_file(row, orig_dir)
    if not op.exists():
        raise FileNotFoundError(f"original {op} is missing; cannot render safely")
    return row, op.read_bytes(), events


def choose_score(it, mdb, tpl, overrides):
    """(source, text, raw 0-100 score or None) for an item, or None.
    A user override wins and needs no MDBList lookup; a text override has no numeric value."""
    decimals = tpl.get("decimals", 1)
    if it.id in overrides:
        v = overrides[it.id]
        return "override", metamod.format_score(v, decimals), None if isinstance(v, str) else metamod.override_pct(v)
    if mdb is None:
        raise SetupError("The MDBList API key is not set, so there are no scores to draw. Add it under Settings, MDBList, press Save and test, then run this again.")
    kind = "movie" if it.type == "Movie" else "show"
    for prov in ("imdb", "tmdb", "tvdb"):
        if prov in it.provider_ids:
            data = mdb.lookup(prov, kind, it.provider_ids[prov])
            if data:
                return pick_score(data, tpl["sources"], decimals)
    return None


def sync_critic_rating(srv, store, it, pct) -> list[Event]:
    """Keep the server's CriticRating (0-100) equal to the score we show. Same safety as metadata
    overrides: backup, lock, read-back, and edits made in the server are left alone."""
    pct = int(pct)
    plan = metamod.plan_item(srv.get_full(it.id), {"critic_rating": pct}, store.meta_get(it.id), lockable=getattr(srv, "lockable", None))
    c = plan.changes[0]
    if c.action == "ok":
        return []
    if c.action == "hand-edit":
        return [Event("skip", it.name, f"CriticRating was edited in the server ({_short(c.old)}); left alone", it.id)]
    r = metamod.apply_item(srv, store, plan)
    if r.ok:
        return [Event("critic", it.name, f"CriticRating {_short(c.old)} -> {pct}", it.id)]
    return [Event("warn", it.name, f"CriticRating not kept by the server: {'; '.join(r.messages)}", it.id)]


# ---- actions ---------------------------------------------------------------------------------------------
def render_posters(ws: Workspace, tpl: dict, overrides_path: Path, required: bool = False,
                   limit: int | None = None) -> Iterator[Event]:
    """Dry run: write overlays to <data>/out and upload nothing."""
    items, overrides, events, per = poster_context(ws, overrides_path, required)
    yield from events
    assets = bp.AssetIndex(ws.assets)
    tcache: dict = {}
    for it in items[:limit]:
        if not it.has_poster and assets.find(it) is None:  # a custom poster can stand in for a missing one
            yield Event("skip", it.name, "no poster", it.id)
            continue
        ps = per.get(it.id, {})
        try:
            tpl_i, _ = item_template(ws, tpl, "", ps, tcache)
        except ConfigError as e:
            yield Event("error", it.name, str(e), it.id)
            continue
        if ps.get("overlay") is False:
            yield Event("skip", it.name, "no score on this title (overlay: false)", it.id)
            continue
        try:
            _, original, ev = get_original(ws.srv, ws.store, ws.originals, it, assets.find(it))
        except (FileNotFoundError, Unsure) as e:
            yield Event("error", it.name, str(e), it.id)
            continue
        yield from ev
        pick = choose_score(it, ws.mdb, tpl_i, overrides)
        if not pick:
            yield Event("skip", it.name, "no score from MDBList", it.id)
            continue
        src, text, _ = pick
        (ws.out / f"{_safe(it.name)}_{it.id}.jpg").write_bytes(render(original, text, tpl_i))
        yield Event("render", f"{it.name} ({it.year})", f"{src} {text}", it.id)


def item_template(ws: Workspace, tpl: dict, sig: str, ps: dict, cache: dict) -> tuple[dict, str]:
    """The template for one title: the run's own, or the one an override names, with the title's score source applied.
    Raises ConfigError with a sentence when the named template cannot be used."""
    name = ps.get("template")
    if name:
        if name not in cache:
            try:
                cache[name] = load_template(name, ws.root)
            except (FileNotFoundError, ValueError, OSError, KeyError, TypeError) as e:
                cache[name] = ConfigError(f"template {name!r} for this title could not be loaded ({type(e).__name__})")
        if isinstance(cache[name], Exception):
            raise cache[name]
        tpl, sig = cache[name]
    src = ps.get("source")
    if src:
        tpl = {**tpl, "sources": [src] if src == "aggregate" else [src, "aggregate"]}
    return tpl, sig


def asset_changed(row, asset: Path | None) -> bool:
    """True when a custom poster exists that is not (yet) the item's base poster."""
    if asset is None:
        return False
    if row is None:
        return True
    try:
        return sha(bp.load(asset)) != row["original_sha"]
    except bp.AssetError:
        return True  # let the full path report what is wrong with the file


def unchanged(row, it) -> bool:
    """True when we already applied an overlay and the server still serves exactly the poster we recorded."""
    return bool(row and row["applied_hash"] is not None and row["served_sha"] and row["served_tag"] == it.poster_tag)


def apply_posters(ws: Workspace, tpl: dict, tpl_sig: str, overrides_path: Path, required: bool = False,
                  item: str | None = None, critic: bool = False, only_changed: bool = False,
                  only_ids: set | None = None) -> Iterator[Event]:
    """only_changed: handle just new items and items whose poster changed on the server (a new poster
    you picked, or an overlay that was wiped). Unchanged items are skipped before any per-item request, so a
    daily run over a big library is cheap. It does NOT refresh scores or notice template/override edits:
    run without it after changing those."""
    items, overrides, events, per = poster_context(ws, overrides_path, required)
    yield from events
    if critic:
        yield Event("info", message="critic rating: on (the score is also written to the server's CriticRating, locked)")
    skipped = 0
    assets = bp.AssetIndex(ws.assets)
    tcache: dict = {}
    for it in items:
        if not _matches(item, it.name, it.id):
            continue
        if only_ids is not None and it.id not in only_ids:  # a chosen set of titles (the Library filter's "apply to these")
            continue
        asset = assets.find(it)
        ps = per.get(it.id, {})
        plain = ps.get("overlay") is False
        row0 = ws.store.get(it.id)
        if (only_changed and it.has_poster and unchanged(row0, it) and not asset_changed(row0, asset)
                and not (plain and row0 is not None and row0["applied_hash"] is not None)):
            skipped += 1
            continue
        if not it.has_poster and asset is None:  # a custom poster can stand in for a missing one (fan edits often have none)
            yield Event("skip", it.name, "no poster", it.id)
            continue
        try:
            tpl_i, sig_i = item_template(ws, tpl, tpl_sig, ps, tcache)
        except ConfigError as e:
            yield Event("error", it.name, str(e), it.id)
            continue
        try:
            row, original, ev = get_original(ws.srv, ws.store, ws.originals, it, asset)
        except (FileNotFoundError, Unsure) as e:
            yield Event("error", it.name, str(e), it.id)
            continue
        yield from ev
        manual_critic = "critic_rating" in ps  # a CriticRating set by hand in overrides.yaml wins: the automatic write stays away
        if plain:  # overlay: false: the title shows its base poster (the original, or your custom poster) without a score
            adopted = any(e.kind == "new" for e in ev)
            if adopted or row["applied_hash"] is not None or row["served_sha"] != row["original_sha"]:
                ws.srv.upload_primary(it.id, original, _ctype(original))
                back, _ = ws.srv.download_primary(it.id)
                ws.store.set_applied(it.id, None, None, ws.srv.item(it.id).poster_tag, sha(back))
                yield Event("apply", it.name, "no score on this title (as you asked); the base poster is on the server", it.id)
            else:
                yield Event("same", it.name, "no score on this title (as you asked)", it.id)
            if critic and not manual_critic:
                pk = choose_score(it, ws.mdb, tpl_i, overrides)
                if pk and pk[2] is not None:
                    yield from sync_critic_rating(ws.srv, ws.store, it, pk[2])
            continue
        pick = choose_score(it, ws.mdb, tpl_i, overrides)
        if not pick:
            if row["applied_hash"] is not None and (row["score_text"] or "") in ZERO_BADGES:
                # an earlier version drew a badge reading 0.0 for a title that has no real score: take it off again
                ws.srv.upload_primary(it.id, original, _ctype(original))
                back = ws.srv.download_primary(it.id)[0]
                ws.store.set_applied(it.id, None, None, ws.srv.item(it.id).poster_tag, sha(back))
                yield Event("restore", it.name, "a 0.0 badge had been drawn for a title with no real score; the original poster is back", it.id)
                yield from restore_metadata(ws, only_ids={it.id}, keys={"critic_rating"})
            else:
                yield Event("skip", it.name, "no score from MDBList", it.id)
            continue
        src, text, pct = pick
        if critic and pct is not None and not manual_critic:
            yield from sync_critic_rating(ws.srv, ws.store, it, pct)
        h = sha((row["original_sha"] + src + text + sig_i + RENDER_VERSION).encode())
        if row["applied_hash"] == h:
            yield Event("same", it.name, f"{src} {text} already applied", it.id)
            continue
        rendered = render(original, text, tpl_i)
        ws.srv.upload_primary(it.id, rendered, "image/jpeg")
        back, _ = ws.srv.download_primary(it.id)
        ok = abs(len(back) - len(rendered)) < 0.2 * len(rendered) and sha(back) != row["original_sha"]
        ws.store.set_applied(it.id, h, text, ws.srv.item(it.id).poster_tag, sha(back))
        was = row["score_text"] if row["applied_hash"] is not None and row["score_text"] and row["score_text"] != text else None
        yield Event("apply" if ok else "warn", it.name, f"{src} {f'{was} -> ' if was else ''}{text}" + (
            "" if ok else " (read-back differs from expectation; check in the server)"), it.id)
    if item is None and ws.scfg.library is None:  # a pass over every library just completed
        schedule.mark(ws.data, *(("changed",) if only_changed else ("changed", "full")))
    if only_changed:
        yield Event("info", message=f"only new or changed posters: {skipped} unchanged item{'' if skipped == 1 else 's'} skipped "
                                    "(scores, template and override edits are not re-checked in this mode)")


def restore_posters(ws: Workspace, item: str | None = None, only_ids: set | None = None) -> Iterator[Event]:
    for row in ws.store.all():
        if not _matches(item, row["name"], row["item_id"]):
            continue
        if only_ids is not None and row["item_id"] not in only_ids:
            continue
        if row["applied_hash"] is None:
            yield Event("clean", row["name"], "already original", row["item_id"])
            continue
        data = original_file(row, ws.originals).read_bytes()
        if sha(data) != row["original_sha"]:
            yield Event("error", row["name"], "saved original changed on disk; refusing", row["item_id"])
            continue
        ws.srv.upload_primary(row["item_id"], data, _ctype(data))
        back = ws.srv.download_primary(row["item_id"])[0]
        ws.store.set_applied(row["item_id"], None, None, ws.srv.item(row["item_id"]).poster_tag, sha(back))
        yield Event("restore", row["name"], "", row["item_id"])


def restore_metadata(ws: Workspace, item: str | None = None, force: bool = False, only_ids: set | None = None,
                     keys: set | None = None) -> Iterator[Event]:
    ids = []
    for row in ws.store.meta_all():
        if only_ids is not None and row["item_id"] not in only_ids:
            continue
        if keys is not None and row["key"] not in keys:
            continue
        if _matches(item, row["name"], row["item_id"]) and row["item_id"] not in ids:
            ids.append(row["item_id"])
    for iid in ids:
        r = metamod.restore_item(ws.srv, ws.store, iid, force, keys)
        if r.restored:
            yield Event("restore", r.name, f"{', '.join(r.restored)} back to the original, lock removed", iid)
        for m in r.messages:
            yield Event("warn", r.name, m, iid)


def summarize(events: list[Event]) -> Event:
    """One line for the end of a maintenance run."""
    n = lambda k: sum(e.kind == k for e in events)
    changed_scores = sum(e.kind == "apply" and "->" in e.message for e in events)
    return Event("info", message=f"summary: {n('apply')} poster(s) updated ({changed_scores} because the score changed), "
                                 f"{n('critic')} critic rating(s) updated, {n('same')} unchanged, "
                                 f"{n('lost') + n('new')} poster change(s) found, {n('warn') + n('error')} problem(s)")
