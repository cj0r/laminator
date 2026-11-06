"""Copies of one title: the same movie or show present more than once in a library, for example an HD and a UHD folder.

Each copy is a separate item on the server with its own poster, so every action in laminator keeps working per copy. This module only
*groups* them for display: two items are copies of each other when they are the same type and share an IMDb, TMDb or TVDB id. An item
with no provider id is never grouped (fan edits often share a name and must not be merged by guesswork). Groups are made within one
library."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ID_KEYS = ("imdb", "tmdb", "tvdb")


@dataclass
class Group:
    primary: object                                   # the copy shown for the whole group
    copies: list = field(default_factory=list)        # every copy, the primary included, in list order


def _tokens(it) -> list[tuple]:
    kind = getattr(it, "type", "") or ""
    ids = getattr(it, "provider_ids", None) or {}
    out = []
    for k in ID_KEYS:
        v = ids.get(k)
        if v not in (None, "", 0, "0"):
            out.append((kind, k, str(v).strip().lower()))
    return out


def group_items(items: list) -> list[Group]:
    """Group `items` (keeping their order: a group sits where its first copy was). A copy that shares an id with any other copy joins it,
    so items that each know a different one of the ids are still brought together through a third that knows both."""
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    seen: dict[tuple, int] = {}
    for n, it in enumerate(items):
        for tok in _tokens(it):
            if tok in seen:
                a, b = find(n), find(seen[tok])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                seen[tok] = n
    members: dict[int, list] = {}
    for n, it in enumerate(items):
        members.setdefault(find(n), []).append(it)
    groups = []
    for n in sorted(members):
        copies = members[n]
        primary = next((c for c in copies if getattr(c, "has_poster", True)), copies[0])
        groups.append(Group(primary, copies))
    return groups


def group_of(items: list, item_id: str) -> Group | None:
    for g in group_items(items):
        if any(c.id == item_id for c in g.copies):
            return g
    return None


def copy_label(it, n: int = 0) -> str:
    """Which copy this is, from where its media lives: the last two folders (movies-uhd/1917 (2019)). 'copy N' when the server gave no path."""
    p = (getattr(it, "path", None) or "").replace("\\", "/").rstrip("/")
    parts = [x for x in p.split("/") if x]
    if parts and getattr(it, "type", "") == "Movie" and "." in parts[-1]:
        parts = parts[:-1]  # a movie's path is its file: the folder it is in is what tells the copies apart
    if not parts:
        return f"copy {n}" if n else "copy"
    parts = [re.sub(r"\s*[\[{](?:imdb|tmdb|tvdb)id?-[^\]}]*[\]}]", "", x).strip() or x for x in parts]  # drop [imdbid-tt123] style tags
    return "/".join(parts[-2:])
