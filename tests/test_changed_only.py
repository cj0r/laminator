import io
from pathlib import Path
from types import SimpleNamespace as NS

from PIL import Image

from laminator import actions, assets
from laminator.store import Store


def jpeg(color):
    b = io.BytesIO(); Image.new("RGB", (200, 300), color).save(b, "JPEG"); return b.getvalue()


class Srv:
    def __init__(self, items):
        self.items_, self.posters, self.calls = items, {i.id: jpeg((10, 10, 10)) for i in items}, []

    def libraries(self): return [{"Name": "Movies", "CollectionType": "movies"}]
    def items(self, lib=None, limit=None): return self.items_
    def download_primary(self, i, max_width=None): return self.posters[i], "image/jpeg"
    def upload_primary(self, i, data, ctype="image/jpeg"): self.calls.append(("upload", i)); self.posters[i] = data
    def item(self, i): return next(x for x in self.items_ if x.id == i)
    def get_full(self, i): self.calls.append(("get_full", i)); return {"Id": i, "Name": "n", "CriticRating": None, "LockedFields": []}
    def post_item(self, i, body): pass


class Mdb:
    remaining = None

    def __init__(self): self.lookups = []

    def lookup(self, prov, kind, pid):
        self.lookups.append(pid); return {"score": 73, "ratings": []}


def item(i, tag):
    return NS(id=i, name=f"Item{i}", type="Movie", year=2000, provider_ids={"imdb": f"tt{i}"}, has_poster=True, poster_tag=tag)


def run(tmp_path, srv, mdb, **kw):
    store = Store(tmp_path / "s.db")
    ws = actions.Workspace({}, NS(library=None, kind="emby"), srv, store, mdb, tmp_path)
    tpl, sig = assets.load_template("default", tmp_path)
    return list(actions.apply_posters(ws, tpl, sig, tmp_path / "none.yaml", False, **kw)), store


def test_unchanged_items_cost_nothing_and_new_or_changed_ones_are_processed(tmp_path):
    a, b = item("1", "ta"), item("2", "tb")
    srv, mdb = Srv([a, b]), Mdb()
    run(tmp_path, srv, mdb)                                   # first full run: both new -> both get an overlay
    assert {c for c in srv.calls if c[0] == "upload"} == {("upload", "1"), ("upload", "2")}
    for i in (a, b):                                          # the server's tags now match what we recorded
        i.poster_tag = Store(tmp_path / "s.db").get(i.id)["served_tag"]
    srv.calls.clear(); mdb.lookups.clear()

    events, _ = run(tmp_path, srv, mdb, only_changed=True, critic=True)
    assert srv.calls == [] and mdb.lookups == []              # nothing touched: no uploads, no get_full, no MDBList
    assert [e.kind for e in events] == ["info", "info"] and "2 unchanged items skipped" in events[-1].message

    c = item("3", "tc"); srv.items_.append(c); srv.posters["3"] = jpeg((200, 0, 0))   # a brand new item
    srv.posters["1"] = jpeg((0, 200, 0)); a.poster_tag = "changed-by-user"           # the user picked a new poster
    srv.calls.clear(); mdb.lookups.clear()
    events, _ = run(tmp_path, srv, mdb, only_changed=True)
    kinds = [(e.kind, e.name) for e in events if e.kind in ("apply", "new", "same")]
    assert ("apply", "Item3") in kinds and ("apply", "Item1") in kinds and ("new", "Item1") in kinds
    assert "Item2" not in [n for _, n in kinds]               # the untouched one stayed untouched
    assert sorted(mdb.lookups) == ["tt1", "tt3"]


def test_a_full_run_still_visits_everything(tmp_path):
    a = item("1", "ta"); srv, mdb = Srv([a]), Mdb()
    run(tmp_path, srv, mdb)
    a.poster_tag = Store(tmp_path / "s.db").get("1")["served_tag"]
    mdb.lookups.clear()
    events, _ = run(tmp_path, srv, mdb)
    assert [e.kind for e in events] == ["same"] and mdb.lookups == ["tt1"]
