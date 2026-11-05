"""Metadata overrides: plan (diff), apply, restore. Returns structured results; no printing,
so a CLI or a web UI can render them.

Rules (all verified against Emby 4.10.2, see README):
- an override is written together with a lock on exactly that field, so a refresh can't undo it;
- sort titles only take effect when SortName is locked;
- Emby answers 204 even when it ignores a field, so every write is read back;
- values the user edits by hand in Emby are detected and left alone unless forced.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .mdblist import score_pct, score_text

# override key -> (item DTO field, LockedFields name): the four plain text fields (kept for callers that only need those)
FIELDS = {
    "rating": ("OfficialRating", "OfficialRating"),
    "sort_title": ("ForcedSortName", "SortName"),
    "original_title": ("OriginalTitle", "OriginalTitle"),
    "overview": ("Overview", "Overview"),
}


@dataclass(frozen=True)
class Spec:
    """How one overridable field is read from and written to the server's item."""
    dto: str
    lock: str | None  # the LockedFields name that protects it from a refresh; None when the server cannot lock it
    kind: str         # text | tagline | int | names | studios
    label: str = ""


# Verified against Emby 4.10.2 (see docs/emby-notes.md): Tags and Genres are only written when their ...Items twin is sent too, Studios
# are objects, Taglines is a list and cannot be locked, Name/Tags/Genres/Studios/CriticRating lock under their own names.
SPEC = {
    "rating": Spec("OfficialRating", "OfficialRating", "text", "content rating"),
    "sort_title": Spec("ForcedSortName", "SortName", "text", "sort title"),
    "original_title": Spec("OriginalTitle", "OriginalTitle", "text", "original title"),
    "overview": Spec("Overview", "Overview", "text", "description"),
    "title": Spec("Name", "Name", "text", "title"),
    "tagline": Spec("Taglines", None, "tagline", "tagline"),
    "critic_rating": Spec("CriticRating", "CriticRating", "int", "critic rating"),
    "tags": Spec("Tags", "Tags", "names", "tags"),
    "genres": Spec("Genres", "Genres", "names", "genres"),
    "studios": Spec("Studios", "Studios", "studios", "studios"),
}
LIST_KEYS = {k for k, sp in SPEC.items() if sp.kind in ("names", "studios")}
MAX_TEXT = {"rating": 40, "sort_title": 200, "original_title": 200, "title": 200, "tagline": 300, "overview": 5000}
MAX_LIST_ITEMS, MAX_LIST_ITEM_LEN = 100, 120
CLEARABLE = {"overview", "tagline"}  # an empty text means "clear it" only where that makes sense

# the fields only laminator itself uses (the poster side); never sent to the server
POSTER_KEYS = {"score", "template", "source", "overlay"}
MAX_SCORE_TEXT = 8
_ALL = {k: (sp.dto, sp.lock) for k, sp in SPEC.items()}   # every key a stored backup row may carry
INTERNAL_FIELDS = {"critic_rating": _ALL["critic_rating"]}  # written by laminator itself from the score unless an override says otherwise


class OverrideError(Exception):
    pass


@dataclass
class Change:
    key: str
    old: object
    new: object
    action: str  # change | reapply | lock | ok | hand-edit
    note: str = ""


@dataclass
class ItemPlan:
    item_id: str
    name: str
    changes: list[Change] = field(default_factory=list)
    error: str | None = None
    absent: str = ""   # the other server(s) this entry's title is on, when it is not on this one (not an error: servers need not mirror each other)

    @property
    def writes(self) -> list[Change]:
        return [c for c in self.changes if c.action in ("change", "reapply", "lock")]


# ---- overrides file -------------------------------------------------------------
def load_overrides(path: Path, required: bool = True) -> list[dict]:
    if not path.exists():
        if not required:
            return []
        raise OverrideError(f"overrides file not found: {path}")
    return parse_overrides(path.read_text(), str(path))


def parse_overrides(text: str, where: str = "overrides.yaml") -> list[dict]:
    """Validate the overrides text (the file's contents, or what the web editor is about to save). An empty file means no overrides."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise OverrideError(f"{where}: not valid YAML: {str(e).splitlines()[0] if str(e) else e}")
    if data is None:
        return []
    if not isinstance(data, dict):
        raise OverrideError(f"{where}: the top level must be a mapping with an 'overrides:' list")
    entries = data.get("overrides")
    if entries is None and not data:
        return []
    if not isinstance(entries, list):
        raise OverrideError(f"{where}: expected a top-level 'overrides:' list")
    for i, e in enumerate(entries, 1):
        if not isinstance(e, dict) or not isinstance(e.get("match"), dict) or not e["match"]:
            raise OverrideError(f"override #{i}: needs a 'match' mapping")
        allowed = set(SPEC) | POSTER_KEYS
        bad = set(e) - allowed - {"match", "server"}
        if bad:
            raise OverrideError(f"override #{i}: unknown field(s) {sorted(bad)}; allowed: {sorted(allowed)}")
        if not set(e) & allowed:
            raise OverrideError(f"override #{i}: nothing to change")
        if "server" in e and e["server"] not in ("emby", "jellyfin"):
            raise OverrideError(f"override #{i}: server must be emby or jellyfin, got {e['server']!r}")
        if "score" in e:
            check_score(e["score"], f"override #{i}")
        for k in SPEC:
            if k in e:
                check_value(k, e[k], f"override #{i}")
        _check_poster_keys(e, f"override #{i}")
    return entries


def _bad_text(v: str) -> bool:
    return any(ord(c) < 32 and c not in "\n\r\t" for c in v)


def check_value(key: str, v, where: str) -> None:
    """Validate one field's value as written in the file or typed in the form. Raises OverrideError with a sentence."""
    sp = SPEC[key]
    if sp.kind in ("text", "tagline"):
        if not isinstance(v, str):
            raise OverrideError(f"{where}: {key} must be text, got {type(v).__name__}")
        if _bad_text(v):
            raise OverrideError(f"{where}: {key} contains control characters")
        if len(v) > MAX_TEXT[key]:
            raise OverrideError(f"{where}: {key} is longer than {MAX_TEXT[key]} characters")
        if not v.strip() and key not in CLEARABLE:
            raise OverrideError(f"{where}: {key} cannot be empty")
    elif sp.kind == "int":
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 100:
            raise OverrideError(f"{where}: {key} must be a whole number from 0 to 100, got {v!r}")
    else:  # a list: the whole list, or {add: [...], remove: [...]}
        ops = {"replace": v} if isinstance(v, list) else v
        if isinstance(v, dict):
            if not v or set(v) - {"add", "remove"}:
                raise OverrideError(f"{where}: {key} takes a list, or add: / remove: lists")
        elif not isinstance(v, list):
            raise OverrideError(f"{where}: {key} must be a list (or add: / remove: lists), got {type(v).__name__}")
        for op, lst in ops.items():
            if not isinstance(lst, list) or len(lst) > MAX_LIST_ITEMS:
                raise OverrideError(f"{where}: {key} {op} must be a list of at most {MAX_LIST_ITEMS} names")
            for x in lst:
                if not isinstance(x, str) or not x.strip() or len(x) > MAX_LIST_ITEM_LEN or _bad_text(x):
                    raise OverrideError(f"{where}: {key} names must be text of 1-{MAX_LIST_ITEM_LEN} characters, got {x!r}")


def _check_poster_keys(e: dict, where: str) -> None:
    from .assets import SOURCE_IDS
    if "template" in e and not (isinstance(e["template"], str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", e["template"])):
        raise OverrideError(f"{where}: template must be a template's name (lower-case letters, digits and dashes), got {e['template']!r}")
    if "source" in e and e["source"] not in SOURCE_IDS:
        raise OverrideError(f"{where}: source must be one of {', '.join(SOURCE_IDS)}, got {e['source']!r}")
    if "overlay" in e and not isinstance(e["overlay"], bool):
        raise OverrideError(f"{where}: overlay must be true or false, got {e['overlay']!r}")


def list_target(cur: list, v) -> list:
    """The list an override asks for: the list itself, or the current one with names added and removed (order kept, no duplicates)."""
    if isinstance(v, list):
        return list(dict.fromkeys(v))
    drop = set(v.get("remove") or [])
    out = [x for x in cur if x not in drop]
    return out + [x for x in dict.fromkeys(v.get("add") or []) if x not in out and x not in drop]


def get_value(full: dict, key: str):
    """The field's current value on the server, in the plain form used for comparing and for backups."""
    sp = SPEC[key]
    if sp.kind == "text":
        v = full.get(sp.dto)
        return v if v not in ("",) else None
    if sp.kind == "tagline":
        t = full.get("Taglines") or []
        return t[0] if t else None
    if sp.kind == "int":
        return full.get(sp.dto)
    if sp.kind == "studios":
        return [x.get("Name") for x in full.get("Studios") or [] if x.get("Name")]
    items = full.get("TagItems" if key == "tags" else "GenreItems")
    return [x.get("Name") for x in items or [] if x.get("Name")] or list(full.get(sp.dto) or [])


def same(key: str, a, b) -> bool:
    if SPEC[key].kind in ("names", "studios"):
        return sorted(a or []) == sorted(b or [])
    return a == b


def put_value(body: dict, key: str, v) -> None:
    sp = SPEC[key]
    if sp.kind in ("text", "int"):
        body[sp.dto] = v
    elif sp.kind == "tagline":
        body["Taglines"] = [v] if v else []
    elif sp.kind == "studios":
        body["Studios"] = [{"Name": n} for n in (v or [])]
    else:  # Emby only writes the names when the ...Items twin comes with them
        twin = "TagItems" if key == "tags" else "GenreItems"
        body[sp.dto] = list(v or [])
        body[twin] = [{"Name": n} for n in (v or [])]


def show_value(v) -> str:
    """A value as one readable line (lists joined, empty shown as (none))."""
    if v is None or v == [] or v == "":
        return "(none)"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def entry_yaml(match: dict, fields: dict, server: str | None = None) -> str:
    """One override as YAML text to append under `overrides:`. Every value goes through json.dumps, whose output is also a valid YAML
    double-quoted scalar, so user text can never break out of its value or inject keys."""
    import json
    lines = ["  - match: {" + ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in match.items()) + "}"]
    if server:
        lines.append(f"    server: {json.dumps(server)}")
    for k, v in fields.items():
        lines.append(f"    {k}: {json.dumps(v)}")
    return "\n".join(lines) + "\n"


def append_entry(text: str, entry: str) -> str:
    """Add an entry to the end of the overrides list WITHOUT touching anything else in the file (comments, order, spacing).
    Only a block-style list can be appended to safely; anything else is refused so the user edits it deliberately."""
    if not text.strip():
        return "overrides:\n" + entry
    lines = text.splitlines()
    idx = [i for i, ln in enumerate(lines) if re.match(r"^overrides:\s*(#.*)?$", ln)]
    if idx:
        return text.rstrip("\n") + "\n" + entry
    flow = [i for i, ln in enumerate(lines) if re.match(r"^overrides:\s*\[\s*\]\s*(#.*)?$", ln)]
    if flow:
        lines[flow[0]] = "overrides:"
        return "\n".join(lines).rstrip("\n") + "\n" + entry
    raise OverrideError("the file's overrides list is not in the plain block style, so a new entry cannot be appended safely: use the editor")


def replace_entry(text: str, index: int, entry: str) -> str:
    """Replace the index-th (0-based) entry of the overrides list with `entry` (from entry_yaml) WITHOUT touching anything else in the
    file: other entries, comments and spacing stay as they are, and a comment at the end of the entry's first line is kept. Only a
    block-style list is handled; the result is re-parsed and must hold the same number of entries with the new one in place, otherwise
    nothing is returned and the caller sends the user to the editor."""
    lines = text.splitlines()
    start0 = next((i for i, ln in enumerate(lines) if re.match(r"^overrides:\s*(#.*)?$", ln)), None)
    if start0 is None:
        raise OverrideError("the file's overrides list is not in the plain block style, so the entry cannot be replaced safely: use the editor")
    starts = [i for i in range(start0 + 1, len(lines)) if re.match(r"^\s*-\s", lines[i])]
    if not starts:
        raise OverrideError("could not find the entry in the file: use the editor")
    indent = len(lines[starts[0]]) - len(lines[starts[0]].lstrip())
    starts = [i for i in starts if len(lines[i]) - len(lines[i].lstrip()) == indent]
    if not 0 <= index < len(starts):
        raise OverrideError("the file changed since the page was opened: reload the item and try again")
    a = starts[index]
    b = starts[index + 1] if index + 1 < len(starts) else len(lines)
    while b > a + 1 and (not lines[b - 1].strip() or lines[b - 1].lstrip().startswith("#")):
        b -= 1  # blank lines and comments after the entry belong to the gap before the next one
    new = entry.rstrip("\n").splitlines()
    m = re.search(r"\}\s*(#.*)$", lines[a])
    if m:
        new[0] = new[0] + "   " + m.group(1)
    out = "\n".join(lines[:a] + new + lines[b:]) + ("\n" if text.endswith("\n") else "")
    before, after = parse_overrides(text), parse_overrides(out)
    if len(before) != len(after) or after[index] != parse_overrides("overrides:\n" + entry)[0] or any(
        x != y for i, (x, y) in enumerate(zip(before, after)) if i != index
    ):
        raise OverrideError("the file's layout made the replacement unreliable: use the editor")
    return out


def matching_entry(entries: list[dict], item, library: str, kind: str | None) -> int | None:
    """Index (in the whole list) of the first entry that applies to this item on this server, or None."""
    for i, e in enumerate(entries):
        if kind is not None and e.get("server") not in (None, kind):
            continue
        try:
            if any(h.id == item.id for h in resolve(e, {library: [item]})):
                return i
        except OverrideError:
            continue
    return None


def for_server(entries: list[dict], kind: str | None) -> list[dict]:
    """An entry may name `server: emby|jellyfin`; without it, it applies to whichever server it matches on."""
    return [e for e in entries if kind is None or e.get("server") in (None, kind)]


def check_score(v, where: str = "score") -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise OverrideError(f"{where}: score must be a number 0-10 or short text, got {v!r}")
    if isinstance(v, str):
        if not v.strip() or len(v.strip()) > MAX_SCORE_TEXT:
            raise OverrideError(f"{where}: score text must be 1-{MAX_SCORE_TEXT} characters, got {v!r}")
    elif not 0 <= v <= 10:
        raise OverrideError(f"{where}: numeric score must be between 0 and 10, got {v}")


def format_score(v, decimals: int = 1) -> str:
    """A number is on the 0-10 scale and formatted like MDBList scores (decimals=0 -> 0-100). Text is shown as is."""
    if isinstance(v, str):
        return v.strip()
    return score_text(override_pct(v), decimals)


def override_pct(v) -> int:
    """A numeric override (0-10) as the same rounded 0-100 unit MDBList scores use."""
    return score_pct(Decimal(str(v)) * 10)


def score_overrides(entries: list[dict], items_by_library: dict[str, list]) -> tuple[dict[str, object], list[str]]:
    """item id -> score override. Entries that match nothing are reported and ignored."""
    out, errors = {}, []
    for e in entries:
        if "score" not in e:
            continue
        try:
            for it in resolve(e, items_by_library):
                out[it.id] = e["score"]
        except OverrideError as err:
            errors.append(str(err))
    return out, errors


def per_item_settings(entries: list[dict], items_by_library: dict[str, list], absent: list | None = None) -> tuple[dict[str, dict], list[str]]:
    """item id -> the settings laminator applies on its own side: score, template, source, overlay, and critic_rating (a manual
    CriticRating means the automatic one is not written). Entries that match nothing are reported and ignored. A title in both an HD and a UHD library gets the settings on both copies."""
    out, errors = {}, []
    for e in entries:
        keys = (POSTER_KEYS | {"critic_rating"}) & set(e)
        if not keys:
            continue
        try:
            hits = resolve(e, items_by_library)
        except OverrideError as err:
            if absent is not None and not e.get("server"):   # several servers: an entry for a title this one lacks is expected, not a mistake
                absent.append(e)
            else:
                errors.append(str(err))
            continue
        for it in hits:
            out.setdefault(it.id, {}).update({k: e[k] for k in keys})
    return out, errors


def resolve(entry: dict, items_by_library: dict[str, list]) -> list:
    """Every item an override matches: usually one, two when the title is in both an HD and a UHD library (add `library:` to
    narrow it). Raises OverrideError when nothing matches."""
    m = {str(k).lower(): str(v) for k, v in entry["match"].items()}
    ids = {k: v for k, v in m.items() if k in ("imdb", "tmdb", "tvdb")}
    if not ids and "title" not in m:
        raise OverrideError(f"match {entry['match']}: use imdb/tmdb/tvdb or library+title(+year)")
    hits = []
    for lib, items in items_by_library.items():
        if "library" in m and m["library"].lower() != lib.lower():
            continue
        for it in items:
            if ids and all(it.provider_ids.get(k, "").lower() == v.lower() for k, v in ids.items()):
                hits.append(it)
            elif not ids and it.name.lower() == m["title"].lower() and (
                "year" not in m or str(it.year) == m["year"]
            ):
                hits.append(it)
    if not hits:
        raise OverrideError(f"no item matches {entry['match']}")
    return hits


def lock_name(key: str, lockable) -> str | None:
    """The LockedFields name that protects this field on a server that accepts `lockable` names (None = all); None if it cannot be locked there."""
    name = SPEC[key].lock
    return name if name and (lockable is None or name in lockable) else None


# ---- planning ---------------------------------------------------------------------
def plan_item(full: dict, desired: dict, backups: dict, valid_ratings: list[str] | None = None, lockable=None) -> ItemPlan:
    plan = ItemPlan(str(full["Id"]), full.get("Name", "?"))
    locked = set(full.get("LockedFields") or [])
    for key, want in desired.items():
        sp = SPEC[key]
        if key == "rating" and valid_ratings is not None and want not in valid_ratings:
            plan.error = f"rating {want!r} is not one the server knows ({', '.join(valid_ratings)})"
            return plan
        cur, row = get_value(full, key), backups.get(key)
        new = list_target(cur or [], want) if key in LIST_KEYS else want
        if key in CLEARABLE and new == "":
            new = None  # an empty text means "clear it"
        lk = lock_name(key, lockable)
        locked_ok = lk is None or lk in locked
        if same(key, cur, new):
            note = "" if locked_ok else f"already set; will lock {lk}"
            plan.changes.append(Change(key, cur, new, "ok" if locked_ok else "lock", note))
        elif row is None:
            plan.changes.append(Change(key, cur, new, "change", f"will lock {lk}" if lk else "the server cannot lock this field: a refresh can undo it"))
        elif same(key, cur, json.loads(row["override_value"])):
            plan.changes.append(Change(key, cur, new, "change", "override in the file changed"))
        elif same(key, cur, json.loads(row["old_value"])):
            plan.changes.append(Change(key, cur, new, "reapply", "server put the old value back"))
        else:
            plan.changes.append(Change(key, cur, new, "hand-edit",
                                       "edited in the server since we wrote it; left alone (use --force)"))
    return plan


def _on_other_servers(entry: dict, kind: str | None, elsewhere) -> list[str]:
    """The other servers whose libraries hold this entry's title; none when the entry names one server or no other server is known."""
    if elsewhere is None or entry.get("server"):   # an entry limited to this server should match here
        return []
    found = []
    for other, libs in (elsewhere() or {}).items():
        if other == kind:
            continue
        try:
            if resolve(entry, libs):
                found.append(other)
        except OverrideError:
            pass
    return found


FETCH_WORKERS = 8  # parallel reads of the server; enough to hide its latency without hammering it


def plan_all(srv, store, entries: list[dict], only: str | None = None, kind: str | None = None, elsewhere=None) -> list[ItemPlan]:
    """`elsewhere`: a function returning {other server kind: its libraries and items}, called only if some entry matches nothing here.
    An entry that is not limited to this server and matches nothing here but does match on another server is reported as absent
    (the servers do not have to hold the same titles), not as a problem; anything that matches nowhere stays an error."""
    libs = {l["Name"]: srv.items(l["Name"]) for l in srv.libraries() if l.get("CollectionType") in ("movies", "tvshows")}
    slots: list = []   # in file order: an error plan, or (item, desired) waiting for its server data
    for e in for_server(entries, kind):
        desired = {k: e[k] for k in SPEC if k in e}
        if not desired:  # score-only entry: handled by the poster commands
            continue
        try:
            hits = resolve(e, libs)
        except OverrideError as err:
            if only:  # asked about one title: entries that match nothing are not part of the answer
                continue
            there = _on_other_servers(e, kind, elsewhere)
            slots.append(ItemPlan("", str(e["match"]), absent=", ".join(there)) if there else ItemPlan("", str(e["match"]), error=str(err)))
            continue
        for it in hits:
            if only and only.lower() != it.name.lower() and only != it.id:
                continue
            slots.append((it, desired))
    work = [s for s in slots if isinstance(s, tuple)]

    def fetch(job):
        it, desired = job
        return srv.get_full(it.id), (srv.rating_names(it.id) if "rating" in desired else None)

    if len(work) > 1:  # one request per title: a few hundred titles take seconds one after the other, a fraction of that side by side
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            fetched = iter(list(pool.map(fetch, work)))
    else:
        fetched = iter([fetch(j) for j in work])
    plans = []
    for s in slots:
        if isinstance(s, ItemPlan):
            plans.append(s)
            continue
        it, desired = s
        full, ratings = next(fetched)
        plans.append(plan_item(full, desired, store.meta_get(it.id), ratings, getattr(srv, "lockable", None)))
    return plans


# ---- apply ---------------------------------------------------------------------------
@dataclass
class Result:
    item_id: str
    name: str
    ok: bool
    messages: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)


def apply_item(srv, store, plan: ItemPlan, force: bool = False) -> Result:
    res = Result(plan.item_id, plan.name, True)
    todo = list(plan.writes) + ([c for c in plan.changes if c.action == "hand-edit"] if force else [])
    if plan.error or not todo:
        return res
    full = srv.get_full(plan.item_id)
    backups = store.meta_get(plan.item_id)
    locks = list(full.get("LockedFields") or [])
    lockable = getattr(srv, "lockable", None)
    body = dict(full)
    for c in todo:
        lk = lock_name(c.key, lockable)
        row = backups.get(c.key)
        old = json.loads(row["old_value"]) if row else get_value(full, c.key)
        lock_added = bool(row["lock_added"]) if row else bool(lk and lk not in locks)
        store.meta_put(plan.item_id, plan.name, c.key, old, c.new, lock_added)  # backup BEFORE writing
        put_value(body, c.key, c.new)
        if lk and lk not in locks:
            locks.append(lk)
    body["LockedFields"] = locks
    srv.post_item(plan.item_id, body)
    back = srv.get_full(plan.item_id)  # verify: Emby may silently ignore fields
    back_locks = back.get("LockedFields") or []
    for c in todo:
        lk = lock_name(c.key, lockable)
        now = get_value(back, c.key)
        lock_ok = lk is None or lk in back_locks
        if not same(c.key, now, c.new) or not lock_ok:
            res.ok = False
            res.messages.append(f"{c.key}: server did not keep {show_value(c.new)!r} (now {show_value(now)!r}, "
                                f"lock {'present' if lock_ok else 'missing'})")
    return res


# ---- restore ------------------------------------------------------------------------------
def restore_item(srv, store, item_id: str, force: bool = False, keys: set | None = None) -> Result:
    """Put an item's overridden values back. With `keys`, only those fields (the rest stay as they are)."""
    rows = {k: v for k, v in store.meta_get(item_id).items() if keys is None or k in keys}
    name = next(iter(rows.values()))["name"] if rows else item_id
    res = Result(item_id, name, True)
    if not rows:
        return res
    full = srv.get_full(item_id)
    body = dict(full)
    done, unlock = [], []
    for key, row in rows.items():
        sp = SPEC[key]
        cur, old, ovr = get_value(full, key), json.loads(row["old_value"]), json.loads(row["override_value"])
        if same(key, cur, ovr):
            put_value(body, key, old)
        elif not same(key, cur, old) and not force:
            res.messages.append(f"{key}: edited in the server since we wrote it (now {show_value(cur)!r}); left alone (use --force)")
            continue
        else:
            put_value(body, key, old)
        done.append(key)
        if row["lock_added"] and lock_name(key, getattr(srv, "lockable", None)):
            unlock.append(lock_name(key, getattr(srv, "lockable", None)))
    if done:
        srv.post_item(item_id, body)  # values first: a sort title needs its lock while being written
        locks = [l for l in (full.get("LockedFields") or []) if l not in unlock]
        body2 = dict(srv.get_full(item_id)); body2["LockedFields"] = locks
        srv.post_item(item_id, body2)
        back = srv.get_full(item_id)
        for key in done:
            if not same(key, get_value(back, key), json.loads(rows[key]["old_value"])):
                res.ok = False
                res.messages.append(f"{key}: restore did not stick (now {show_value(get_value(back, key))!r})")
            else:
                store.meta_delete(item_id, key)
                res.restored.append(key)
    return res
