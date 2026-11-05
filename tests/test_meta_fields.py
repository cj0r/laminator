import copy
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml

from laminator import actions, meta
from laminator.config import ServerConfig
from laminator.meta import OverrideError, apply_item, plan_item, restore_item
from laminator.store import Store

BASE = {"Id": "8", "Name": "Elf", "OfficialRating": "PG", "ForcedSortName": "Elf", "OriginalTitle": "Elf", "Overview": "Buddy.",
        "Taglines": ["Discover your inner elf."], "Tags": None, "TagItems": [], "CriticRating": 73,
        "Genres": ["Comedy", "Family", "Fantasy"], "GenreItems": [{"Name": n, "Id": i} for i, n in ((1, "Comedy"), (2, "Family"), (3, "Fantasy"))],
        "Studios": [{"Name": "New Line", "Id": 9}], "LockedFields": ["CriticRating"]}
VALID_LOCKS = {"Name", "Overview", "OfficialRating", "SortName", "OriginalTitle", "CriticRating", "Cast", "Genres", "Tags", "Studios", "Runtime"}


class FakeEmby:
    """Behaves like the Emby 4.10 server in docs/emby-notes.md: tags and genres only change when their ...Items twin is sent, studios are
    objects, a tagline can be written but not locked (unknown lock names are dropped), and an update answers 204 whatever it did."""

    def __init__(self):
        self.item = copy.deepcopy(BASE)

    def get_full(self, _id): return copy.deepcopy(self.item)

    def post_item(self, _id, body):
        it = self.item
        for k in ("Name", "OfficialRating", "OriginalTitle", "Overview", "CriticRating", "Taglines"):
            it[k] = body[k] if k in body else it.get(k)
        it["ForcedSortName"] = body.get("ForcedSortName", it["ForcedSortName"]) if "SortName" in (body.get("LockedFields") or []) else it["ForcedSortName"]
        for twin, key in (("TagItems", "Tags"), ("GenreItems", "Genres")):
            if twin in body:
                it[twin] = [{"Name": x["Name"], "Id": 100 + n} for n, x in enumerate(body[twin])]
                it[key] = None if key == "Tags" else [x["Name"] for x in body[twin]]
        if "Studios" in body:
            it["Studios"] = [{"Name": x["Name"], "Id": 200 + n} for n, x in enumerate(body["Studios"])]
        it["LockedFields"] = [l for l in (body.get("LockedFields") or []) if l in VALID_LOCKS]


@pytest.fixture
def world(tmp_path):
    return FakeEmby(), Store(tmp_path / "s.db")


def run(world, desired):
    srv, store = world
    plan = plan_item(srv.get_full("8"), desired, store.meta_get("8"))
    return plan, apply_item(srv, store, plan)


# ---- the file ------------------------------------------------------------------------------------------------------
def test_new_fields_are_valid_in_the_file(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("""overrides:
  - match: {imdb: tt1}
    title: "Elf (Fan Edit)"
    tagline: "A fan edit."
    critic_rating: 80
    tags: [Fan Edit, Holiday]
    genres: {add: [Holiday], remove: [Fantasy]}
    studios: []
    overview: ""
    template: my-look
    source: imdb
    overlay: false
""")
    e = meta.load_overrides(f)[0]
    assert e["genres"] == {"add": ["Holiday"], "remove": ["Fantasy"]} and e["overlay"] is False and e["studios"] == []


@pytest.mark.parametrize("line,msg", [
    ("title: ''", "cannot be empty"), ("title: 5", "must be text"), ("tagline: " + "x" * 301, "longer than 300"),
    ("tags: Holiday", "must be a list"), ("tags: [a, 5]", "names must be text"), ("tags: ['']", "names must be text"),
    ("genres: {add: Holiday}", "must be a list of at most"), ("genres: {keep: [a]}", "takes a list, or add: / remove:"),
    ("genres: {}", "takes a list, or add: / remove:"), ("tags: " + str(list(range(101))), "at most 100 names"),
    ("template: ../x", "template must be"), ("template: Big Look", "template must be"), ("source: bogus", "source must be one of"),
    ("overlay: no thanks", "overlay must be true or false"), ("overview: " + "y" * 5001, "longer than 5000"),
])
def test_bad_values_are_refused_with_a_sentence(tmp_path, line, msg):
    f = tmp_path / "o.yaml"
    f.write_text(f"overrides:\n  - match: {{imdb: tt1}}\n    {line}\n")
    with pytest.raises(OverrideError, match=msg):
        meta.load_overrides(f)


def test_control_characters_are_refused(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text('overrides:\n  - match: {imdb: tt1}\n    tags: ["a\\u0007b"]\n')
    with pytest.raises(OverrideError, match="names must be text"):
        meta.load_overrides(f)


def test_a_list_target_replaces_or_adds_and_removes():
    assert meta.list_target(["a", "b"], ["c", "c", "d"]) == ["c", "d"]
    assert meta.list_target(["a", "b", "c"], {"add": ["d", "a"], "remove": ["b"]}) == ["a", "c", "d"]
    assert meta.list_target(["a"], {"remove": ["a"]}) == []
    assert meta.list_target([], {"add": ["x"], "remove": ["x"]}) == []                       # removing wins over adding the same name


def test_entries_can_be_appended_with_every_field_type_and_stay_valid():
    text = meta.entry_yaml({"imdb": "tt1"}, {"title": 'He said "hi"', "tags": ["a: b", "c"], "genres": {"add": ["x"]}, "critic_rating": 70, "overlay": False})
    parsed = yaml.safe_load("overrides:\n" + text)["overrides"][0]
    assert parsed["title"] == 'He said "hi"' and parsed["tags"] == ["a: b", "c"] and parsed["overlay"] is False and parsed["critic_rating"] == 70


# ---- values: reading, comparing, writing ----------------------------------------------------------------------------
def test_values_read_the_way_emby_reports_them():
    assert meta.get_value(BASE, "tags") == [] and meta.get_value(BASE, "genres") == ["Comedy", "Family", "Fantasy"]
    assert meta.get_value(BASE, "studios") == ["New Line"] and meta.get_value(BASE, "tagline") == "Discover your inner elf."
    assert meta.get_value(dict(BASE, Taglines=[]), "tagline") is None and meta.get_value(dict(BASE, Tags=["x"], TagItems=[]), "tags") == ["x"]
    assert meta.same("genres", ["b", "a"], ["a", "b"]) and meta.same("tags", None, []) and not meta.same("rating", "PG", "R")
    assert meta.show_value(["a", "b"]) == "a, b" and meta.show_value([]) == "(none)" and meta.show_value(None) == "(none)" and meta.show_value(73) == "73"


# ---- apply and restore against the Emby-like server --------------------------------------------------------------------
def test_every_new_field_is_written_locked_verified_and_restored_exactly(world):
    srv, store = world
    original = srv.get_full("8")
    plan, res = run(world, {"title": "Elf (Fan Edit)", "tagline": "A fan edit.", "critic_rating": 55, "tags": ["Fan Edit", "Holiday"],
                            "genres": {"add": ["Holiday"], "remove": ["Fantasy"]}, "studios": ["My Studio"]})
    assert res.ok, res.messages
    now = srv.get_full("8")
    assert now["Name"] == "Elf (Fan Edit)" and now["Taglines"] == ["A fan edit."] and now["CriticRating"] == 55
    assert [x["Name"] for x in now["TagItems"]] == ["Fan Edit", "Holiday"] and now["Genres"] == ["Comedy", "Family", "Holiday"]
    assert [x["Name"] for x in now["Studios"]] == ["My Studio"]
    assert set(now["LockedFields"]) == {"CriticRating", "Name", "Tags", "Genres", "Studios"}               # the tagline cannot be locked
    again = plan_item(srv.get_full("8"), {"title": "Elf (Fan Edit)", "tags": ["Fan Edit", "Holiday"], "tagline": "A fan edit."}, store.meta_get("8"))
    assert {c.key: c.action for c in again.changes} == {"title": "ok", "tags": "ok", "tagline": "ok"}   # nothing more to do
    r = restore_item(srv, store, "8")
    assert r.ok and sorted(r.restored) == ["critic_rating", "genres", "studios", "tagline", "tags", "title"], r.messages
    back = srv.get_full("8")
    assert (back["Name"], back["Taglines"], back["CriticRating"], back["Genres"]) == ("Elf", ["Discover your inner elf."], 73, original["Genres"])
    assert back["TagItems"] == [] and [x["Name"] for x in back["Studios"]] == ["New Line"]
    assert back["LockedFields"] == ["CriticRating"] and store.meta_get("8") == {}                          # the lock that was already there stays


def test_the_tagline_note_says_the_server_cannot_lock_it_and_a_refresh_is_caught_as_reverted(world):
    srv, store = world
    plan, res = run(world, {"tagline": "A fan edit."})
    assert res.ok and plan.changes[0].note.startswith("the server cannot lock")
    srv.item["Taglines"] = ["Discover your inner elf."]                                                    # a refresh puts the old one back
    again = plan_item(srv.get_full("8"), {"tagline": "A fan edit."}, store.meta_get("8"))
    assert again.changes[0].action == "reapply"
    assert apply_item(srv, store, again).ok and srv.item["Taglines"] == ["A fan edit."]


def test_clearing_a_tagline_or_a_list_works_and_comes_back(world):
    srv, store = world
    _, res = run(world, {"tagline": "", "genres": [], "studios": []})
    assert res.ok, res.messages
    now = srv.get_full("8")
    assert now["Taglines"] == [] and now["Genres"] == [] and now["Studios"] == []
    assert restore_item(srv, store, "8").ok
    back = srv.get_full("8")
    assert back["Taglines"] == ["Discover your inner elf."] and back["Genres"] == ["Comedy", "Family", "Fantasy"] and len(back["Studios"]) == 1


def test_a_list_edited_by_hand_afterwards_is_a_conflict_not_overwritten(world):
    srv, store = world
    run(world, {"tags": ["Fan Edit"]})
    srv.item["TagItems"] = [{"Name": "Mine", "Id": 1}]                                                      # someone edits tags in the server
    plan = plan_item(srv.get_full("8"), {"tags": ["Fan Edit"]}, store.meta_get("8"))
    assert plan.changes[0].action == "hand-edit" and not plan.writes
    r = restore_item(srv, store, "8")
    assert not r.restored and "edited in the server" in r.messages[0] and srv.item["TagItems"][0]["Name"] == "Mine"
    assert restore_item(srv, store, "8", force=True).restored == ["tags"]


def test_a_server_that_ignores_a_write_is_reported(world):
    srv, store = world
    srv.post_item = lambda _id, body: None                                                                  # a 204 that changes nothing
    _, res = run(world, {"title": "Nope"})
    assert not res.ok and "server did not keep 'Nope'" in res.messages[0]


# ---- per-title settings on the poster side ------------------------------------------------------------------------------
def libs():
    mk = lambda i, n, y, **ids: NS(id=i, name=n, year=y, provider_ids=ids)
    return {"Movies": [mk("1", "Dumbo", 1941, imdb="tt1"), mk("2", "Elf", 2003, imdb="tt2")]}


def test_per_title_settings_resolve_and_report_unmatched_entries():
    entries = [{"match": {"imdb": "tt1"}, "template": "big", "overlay": False, "critic_rating": 70, "score": 9},
               {"match": {"imdb": "tt2"}, "source": "imdb"}, {"match": {"imdb": "tt1"}, "rating": "PG"}, {"match": {"imdb": "nope"}, "overlay": False}]
    per, errors = meta.per_item_settings(entries, libs())
    assert per == {"1": {"template": "big", "overlay": False, "critic_rating": 70, "score": 9}, "2": {"source": "imdb"}} and len(errors) == 1


from test_posters_web import LookupMdb, PosterServer  # noqa: E402


class CriticServer(PosterServer):
    def __init__(self):
        super().__init__()
        self.posted = []
        self.full = {i: {"Id": i, "Name": n, "CriticRating": None, "LockedFields": []} for i, n in (("1", "Elf"), ("2", "<script>alert(1)</script>"))}

    def get_full(self, i): return copy.deepcopy(self.full[i])
    def post_item(self, i, body): self.posted.append(i); self.full[i].update(CriticRating=body.get("CriticRating"), LockedFields=body["LockedFields"])


def workspace(tmp_path, overrides_yaml):
    (tmp_path / "ov.yaml").write_text(overrides_yaml)
    srv = CriticServer()
    ws = actions.Workspace({}, ServerConfig(kind="emby", url="http://x", api_key="k"), srv, Store(tmp_path / "s.db"), LookupMdb(), tmp_path, tmp_path)
    (ws.originals).mkdir(parents=True, exist_ok=True)
    return ws, srv


def apply(ws, tmp_path, **kw):
    from laminator import assets
    tpl, sig = assets.load_template("default", tmp_path)
    return list(actions.apply_posters(ws, tpl, sig, tmp_path / "ov.yaml", critic=True, **kw))


def test_a_manual_critic_rating_stops_the_automatic_write_for_that_title_only(tmp_path):
    ws, srv = workspace(tmp_path, "overrides:\n  - match: {imdb: tt1}\n    critic_rating: 55\n")
    srv.items_[0].provider_ids, srv.items_[1].provider_ids = {"imdb": "tt1"}, {"imdb": "tt2"}
    ev = apply(ws, tmp_path)
    kinds = {}
    for e in ev:
        kinds.setdefault(e.name, []).append(e.kind)
    assert "apply" in kinds["Elf"] and "critic" not in kinds["Elf"]                                    # Elf has its own CriticRating in the file
    assert "critic" in kinds["<script>alert(1)</script>"] and srv.posted == ["2"]                      # the other title is written automatically


def test_overlay_false_puts_the_base_poster_back_without_a_score_and_is_quiet_afterwards(tmp_path):
    ws, srv = workspace(tmp_path, "")
    srv.items_[0].provider_ids = {"imdb": "tt1"}
    ev = apply(ws, tmp_path, item="Elf")
    assert any(e.kind == "apply" and "7.3" in e.message for e in ev)
    drawn = srv.uploaded["1"]
    (tmp_path / "ov.yaml").write_text("overrides:\n  - match: {imdb: tt1}\n    overlay: false\n")
    ev = apply(ws, tmp_path, item="Elf")
    assert any(e.kind == "apply" and "no score on this title" in e.message for e in ev)
    assert srv.uploaded["1"] != drawn and ws.store.get("1")["applied_hash"] is None                     # the original is back, nothing recorded as applied
    ev = apply(ws, tmp_path, item="Elf")
    assert [e.kind for e in ev if e.name == "Elf"] == ["same"]                                         # and the next run leaves it alone
    ev = apply(ws, tmp_path, item="Elf", only_changed=True)
    assert not [e for e in ev if e.name == "Elf" and e.kind == "apply"]


def test_a_per_title_source_and_template_are_used_and_a_missing_template_is_a_clear_error(tmp_path):
    ws, srv = workspace(tmp_path, "overrides:\n  - match: {imdb: tt1}\n    source: imdb\n")
    srv.items_[0].provider_ids = {"imdb": "tt1"}
    ev = apply(ws, tmp_path, item="Elf")
    assert any(e.kind == "apply" and e.message.startswith("aggregate") for e in ev)                    # fake MDBList has no IMDb rating: falls back
    (tmp_path / "ov.yaml").write_text("overrides:\n  - match: {imdb: tt1}\n    template: no-such-look\n")
    ev = apply(ws, tmp_path, item="Elf")
    assert any(e.kind == "error" and "no-such-look" in e.message for e in ev)
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "big-badge.yaml").write_text((Path(__file__).parent.parent / "src/laminator/assets/templates/default.yaml").read_text().replace("size: 0.0433", "size: 0.09"))
    (tmp_path / "ov.yaml").write_text("overrides:\n  - match: {imdb: tt1}\n    template: big-badge\n")
    ev = apply(ws, tmp_path, item="Elf")
    assert any(e.kind == "apply" for e in ev)                                                           # a different template redraws the poster


def test_restoring_only_some_fields_leaves_the_other_overrides_in_place(world):
    srv, store = world
    plan, res = run(world, {"rating": "PG-13", "critic_rating": 55, "sort_title": "Elf 2003"})
    assert res.ok, res.messages
    r = restore_item(srv, store, "8", keys={"critic_rating"})                                          # e.g. remove only the automatic CriticRating
    assert r.ok and r.restored == ["critic_rating"]
    now = srv.get_full("8")
    assert now["CriticRating"] == 73 and now["OfficialRating"] == "PG-13" and now["ForcedSortName"] == "Elf 2003"
    assert sorted(store.meta_get("8")) == ["rating", "sort_title"]                                      # their backups are still there, restorable later
    assert restore_item(srv, store, "8").ok and srv.get_full("8")["OfficialRating"] == "PG"


# ---- Jellyfin only accepts MetadataField names as locks (verified on 10.11.11) ---------------------------------------------------
class FakeJelly(FakeEmby):
    """An update that names an unknown lock fails as a whole with HTTP 400, as Jellyfin does; a sort title is written without any lock."""
    lockable = meta.__dict__.get("JELLY") or frozenset({"Name", "Overview", "OfficialRating", "Genres", "Tags", "Studios", "Cast", "Runtime", "ProductionLocations"})

    def post_item(self, _id, body):
        import httpx
        bad = [l for l in body.get("LockedFields") or [] if l not in self.lockable]
        if bad:
            raise httpx.HTTPStatusError("400", request=httpx.Request("POST", "http://j/Items/8"), response=httpx.Response(400))
        keep = self.item["ForcedSortName"]
        super().post_item(_id, {**body, "LockedFields": [*(body.get("LockedFields") or []), "SortName"]})   # base class writes the sort title only with its lock
        self.item["ForcedSortName"] = body.get("ForcedSortName", keep)
        self.item["LockedFields"] = list(body.get("LockedFields") or [])


def test_jellyfin_gets_sort_title_and_critic_rating_without_a_lock_instead_of_a_400(tmp_path):
    srv, store = FakeJelly(), Store(tmp_path / "s.db")
    srv.item["LockedFields"] = []
    plan = plan_item(srv.get_full("8"), {"sort_title": "Elf 2003", "critic_rating": 80, "rating": "PG-13"}, store.meta_get("8"), lockable=srv.lockable)
    notes = {c.key: c.note for c in plan.changes}
    assert "cannot lock" in notes["sort_title"] and "cannot lock" in notes["critic_rating"] and notes["rating"] == "will lock OfficialRating"
    res = apply_item(srv, store, plan)
    assert res.ok, res.messages                                                       # no 400, and the read-back is satisfied without a lock
    assert srv.item["ForcedSortName"] == "Elf 2003" and srv.item["CriticRating"] == 80 and srv.item["OfficialRating"] == "PG-13"
    assert srv.item["LockedFields"] == ["OfficialRating"]                             # only the lock Jellyfin knows
    again = plan_item(srv.get_full("8"), {"sort_title": "Elf 2003", "critic_rating": 80, "rating": "PG-13"}, store.meta_get("8"), lockable=srv.lockable)
    assert {c.action for c in again.changes} == {"ok"}                                # nothing is forever "lock missing"
    back = restore_item(srv, store, "8")
    assert back.ok, back.messages
    assert srv.item["ForcedSortName"] == "Elf" and srv.item["OfficialRating"] == "PG" and srv.item["LockedFields"] == []


def test_without_the_server_telling_us_its_lock_names_the_old_behaviour_stays(tmp_path):
    emby = FakeEmby()
    assert not hasattr(emby, "lockable") and meta.lock_name("sort_title", None) == "SortName"
    assert meta.lock_name("sort_title", frozenset({"Name"})) is None and meta.lock_name("title", frozenset({"Name"})) == "Name"
    from laminator.server import Emby, Jellyfin
    assert Emby.lockable is None and "SortName" not in Jellyfin.lockable and "CriticRating" not in Jellyfin.lockable and "OfficialRating" in Jellyfin.lockable
