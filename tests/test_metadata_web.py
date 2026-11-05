import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml
from fastapi.testclient import TestClient

from laminator import meta as metamod
from laminator.web.app import create_app
from test_web import FakeMdb, FakeServer, finished, login

CFG = {"servers": {"emby": {"url": "http://x:8096"}}}
RATINGS = ["G", "PG", "PG-13", "R"]


class MetaServer(FakeServer):
    """Remembers what is written, like a real server: values, locks, and a read-back that reflects them."""

    def __init__(self):
        super().__init__()
        self.db = {"1": {"Id": "1", "Name": "Elf", "OfficialRating": "PG", "ForcedSortName": "Elf", "OriginalTitle": "Elf",
                         "Overview": "Buddy.", "CriticRating": None, "LockedFields": []}}
        self.posts = []

    def get_full(self, i): return dict(self.db[i])
    def post_item(self, i, body): self.posts.append(dict(body)); self.db[i] = dict(body)
    def rating_names(self, i): return RATINGS


@pytest.fixture
def ui(tmp_path):
    srv = MetaServer()
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path, srv


def write(root, text):
    (root / "overrides.yaml").write_text(text)


HEAD = "# my notes\noverrides:\n"


def test_the_metadata_page_shows_the_plan_as_old_to_new(ui):
    app, c, tok, root, srv = ui
    write(root, HEAD + "  - match: {imdb: tt1}\n    rating: PG-13\n    sort_title: Elf (2003)\n  - match: {imdb: tt404}\n    rating: R\n")
    html = c.get("/metadata").text
    assert "PG-13" in html and "will change" in html and "Elf (2003)" in html and "no item matches" in html
    assert "Review and apply 2 change" in html and srv.posts == []                       # looking writes nothing
    write(root, "overrides: [broken")
    assert "not valid YAML" in c.get("/metadata").text
    write(root, "")
    assert "No overrides apply to Emby" in c.get("/metadata").text and "Review and apply" not in c.get("/metadata").text


def test_apply_goes_through_a_confirmation_then_writes_locks_and_verifies(ui):
    app, c, tok, root, srv = ui
    write(root, HEAD + "  - match: {imdb: tt1}\n    rating: PG-13\n    sort_title: Elf (2003)\n")
    page = c.get("/metadata/confirm").text
    assert "Apply 2 metadata changes" in page and "PG-13" in page and srv.posts == []
    r = c.post("/metadata/apply", data={"srv": "emby", "token": tok()})
    assert r.status_code == 303
    log = finished(app, c, r.headers["location"])
    assert "written and verified" in log and "rating, sort_title" in log
    full = srv.db["1"]
    assert full["OfficialRating"] == "PG-13" and full["ForcedSortName"] == "Elf (2003)" and set(full["LockedFields"]) == {"OfficialRating", "SortName"}
    again = c.get("/metadata").text
    assert "already set" in again and "Review and apply" not in again                       # idempotent


def test_a_value_you_edited_in_the_server_is_a_conflict_and_only_forced_overwrites(ui):
    app, c, tok, root, srv = ui
    write(root, HEAD + "  - match: {imdb: tt1}\n    rating: PG-13\n")
    finished(app, c, c.post("/metadata/apply", data={"srv": "emby", "token": tok()}).headers["location"])
    srv.db["1"]["OfficialRating"] = "R"                                                    # you changed it by hand in the server
    html = c.get("/metadata").text
    assert "conflict" in html and "left alone" in html and "Overwrite manual edits" in html
    log = finished(app, c, c.post("/metadata/apply", data={"srv": "emby", "token": tok()}).headers["location"])
    assert srv.db["1"]["OfficialRating"] == "R" and "left alone" in log                   # not overwritten
    assert "Apply 1 metadata change" in c.get("/metadata/confirm?item=1&force=1").text and "overwrite values you edited" in c.get("/metadata/confirm?item=1&force=1").text
    log = finished(app, c, c.post("/metadata/apply", data={"srv": "emby", "item": "1", "force": "1", "token": tok()}).headers["location"])
    assert srv.db["1"]["OfficialRating"] == "PG-13" and "written and verified" in log


def test_the_editor_validates_keeps_comments_and_can_undo(ui):
    app, c, tok, root, srv = ui
    assert "overrides:" in c.get("/metadata/edit").text
    good = "# keep this comment\noverrides:\n  - match: {imdb: tt1}   # trailing\n    rating: PG-13\n"
    assert "note=mdsaved" in c.post("/metadata/edit", data={"text": good, "token": tok()}).headers["location"]
    assert (root / "overrides.yaml").read_text() == good                                   # byte for byte, comments included
    for bad, why in (("overrides: [", "not valid YAML"), ("overrides:\n  - match: {imdb: t}\n    colour: red\n", "unknown field"),
                     ("overrides:\n  - match: {imdb: t}\n    rating: 5\n", "must be text"), ("- just\n- a list\n", "top level"),
                     ("overrides:\n  - match: {imdb: t}\n    server: plex\n    rating: R\n", "server must be"), ("x" * 300_000, "larger than")):
        r = c.post("/metadata/edit", data={"text": bad, "token": tok()})
        assert r.status_code == 400 and why in r.text, why
        assert (root / "overrides.yaml").read_text() == good                                # nothing invalid is ever written
    new = good.replace("PG-13", "R")
    c.post("/metadata/edit", data={"text": new, "token": tok()})
    assert (root / "overrides.yaml.prev").read_text() == good
    assert "note=mdundo" in c.post("/metadata/edit/undo", data={"token": tok()}).headers["location"]
    assert (root / "overrides.yaml").read_text() == good


def add(c, tok, **kw):
    return c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), **kw})


def test_add_override_appends_one_correct_entry_without_touching_the_rest(ui):
    app, c, tok, root, srv = ui
    original = "# my notes\n# more notes\noverrides:\n  - match: {imdb: tt9}   # another film\n    rating: R\n"
    write(root, original)
    r = add(c, tok, rating="PG-13", sort_title="Elf (2003)", score="85")
    assert "note=mdadded" in r.headers["location"]
    text = (root / "overrides.yaml").read_text()
    assert text.startswith(original)                                                       # everything before is untouched
    entries = metamod.parse_overrides(text)
    assert len(entries) == 2 and entries[1] == {"match": {"imdb": "tt1"}, "rating": "PG-13", "sort_title": "Elf (2003)", "score": 8.5}
    assert "server" not in entries[1]                                                      # one server configured: no restriction needed
    assert "note=mddup" in add(c, tok, rating="R").headers["location"]                    # no second entry for the same item
    assert len(metamod.parse_overrides((root / "overrides.yaml").read_text())) == 2
    page = c.get("/item/1").text                                                           # the entry exists now: the form edits it
    assert "Override for this item (in overrides.yaml)" in page and "Add an override for this item" not in page
    assert "<option selected>PG-13</option>" in page and 'value="Elf (2003)"' in page and 'name="score" maxlength="8" value="85"' in page


def test_user_text_can_never_break_out_of_its_value(ui):
    app, c, tok, root, srv = ui
    evil = 'x"\n    rating: R\n  - match: {imdb: tt666}\n    overview: "pwned'
    add(c, tok, overview=evil)
    entries = metamod.parse_overrides((root / "overrides.yaml").read_text())
    assert len(entries) == 1 and entries[0]["overview"] == evil and set(entries[0]) == {"match", "overview"}   # one entry, text intact
    write(root, "")
    add(c, tok, sort_title='a: b # c "d" \\ e')
    assert metamod.parse_overrides((root / "overrides.yaml").read_text())[0]["sort_title"] == 'a: b # c "d" \\ e'


def test_add_override_refuses_what_it_cannot_do_safely(ui):
    app, c, tok, root, srv = ui
    assert "note=mdbad" in add(c, tok).headers["location"]                                # nothing filled in
    for kw in ({"rating": "PG-14"}, {"score": "101"}, {"score": "-1"}, {"score": "toolongtext"}, {"sort_title": "x" * 300}, {"overview": "x" * 6000}):
        r = add(c, tok, **kw)
        assert "note=mdbad" in r.headers["location"], kw
    assert not (root / "overrides.yaml").exists()
    write(root, "overrides:\n  - match: {imdb: tt9}\n    rating: R\nother_key: 1\n")             # entry would land under the wrong key
    before = (root / "overrides.yaml").read_text()
    assert "note=mdbad" in add(c, tok, rating="PG").headers["location"] and (root / "overrides.yaml").read_text() == before
    # valid YAML either way, but the new entry would land in `notes`, not `overrides`: only the post-append check catches this
    write(root, "overrides:\n  - match: {imdb: tt9}\n    rating: R\nnotes:\n  - a reminder\n")
    before = (root / "overrides.yaml").read_text()
    assert "note=mdbad" in add(c, tok, rating="PG").headers["location"] and (root / "overrides.yaml").read_text() == before
    write(root, "overrides: {not: a list}\n")
    assert "note=mdbad" in add(c, tok, rating="PG").headers["location"]
    write(root, "overrides: []\n")                                                          # an empty flow list is converted properly
    add(c, tok, rating="PG")
    assert len(metamod.parse_overrides((root / "overrides.yaml").read_text())) == 1
    assert add(c, tok, item="999", rating="PG").status_code == 404
    assert c.post("/metadata/add", data={"srv": "plex", "item": "1", "rating": "PG", "token": tok()}).status_code == 400


def test_with_two_servers_an_added_override_is_limited_to_the_one_you_are_on(tmp_path):
    two = {"servers": {"emby": {"url": "http://a"}, "jellyfin": {"url": "http://b"}}}
    app = create_app(two, {"emby": MetaServer(), "jellyfin": MetaServer()}, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    t = re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    c.post("/metadata/add", data={"srv": "jellyfin", "item": "1", "rating": "R", "token": t})
    assert metamod.parse_overrides((tmp_path / "overrides.yaml").read_text())[0]["server"] == "jellyfin"
    assert "No overrides apply to Emby" in c.get("/metadata?s=emby").text                    # it is for Jellyfin only
    assert "Elf" in c.get("/metadata?s=jellyfin").text and "Review and apply 1 change" in c.get("/metadata?s=jellyfin").text


def test_every_metadata_write_needs_csrf_origin_and_a_password(ui, tmp_path):
    app, c, tok, root, srv = ui
    write(root, HEAD + "  - match: {imdb: tt1}\n    rating: PG-13\n")
    bodies = [("/metadata/edit", {"text": "overrides: []"}), ("/metadata/edit/undo", {}), ("/metadata/add", {"srv": "emby", "item": "1", "rating": "R"}),
              ("/metadata/apply", {"srv": "emby"})]
    for path, body in bodies:
        assert c.post(path, data={**body, "token": ""}).status_code == 403, path
        assert c.post(path, data={**body, "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403, path
    assert srv.posts == [] and "PG-13" in (root / "overrides.yaml").read_text()
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app(dict(CFG), {"emby": MetaServer()}, tmp_path / "ro", None), follow_redirects=False)
    for path, body in bodies:
        assert ro.post(path, data={**body, "token": "x"}).status_code == 403
    assert ro.get("/metadata/edit").status_code == 403 and ro.get("/metadata/confirm").status_code == 403
    page = ro.get("/item/1").text
    assert "Add an override" not in page and ro.get("/metadata").status_code == 200       # the plan is still viewable read-only


# ---- the extra fields on the add-an-override form ----------------------------------------------------------------------
def add(c, tok, **kw):
    return c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), **kw})


def entries(root):
    return yaml.safe_load((root / "overrides.yaml").read_text())["overrides"]


def test_the_form_offers_every_field_with_the_current_value_and_explains_itself(ui):
    app, c, tok, root, srv = ui
    page = c.get("/item/1?s=emby").text
    for label in ("Title", "Content rating", "Critic rating", "Sort title", "Original title", "Tagline", "Description", "Tags", "Genres", "Studios",
                  "Template for this title", "Score source for this title", "No score on this title"):
        assert label in page, label
    assert "now: Elf" in page and "Clear the tagline instead" in page and 'value="clear"' in page and "cannot lock" in page


def test_every_field_type_is_written_to_the_file_in_the_right_shape(ui):
    app, c, tok, root, srv = ui
    (root / "templates").mkdir()
    (root / "templates" / "my-look.yaml").write_text((Path(__file__).parent.parent / "src/laminator/assets/templates/default.yaml").read_text())
    r = add(c, tok, title="Elf (Fan Edit)", critic_rating="80", tagline="A fan edit.", tags="Fan Edit, Holiday", tags_mode="replace",
            genres="Holiday\nFamily", genres_mode="add", studios="Old Studio", studios_mode="remove", template="my-look", source="imdb", no_overlay="1", score="95")
    assert "note=mdadded" in r.headers["location"]
    e = entries(root)[0]
    assert e["title"] == "Elf (Fan Edit)" and e["critic_rating"] == 80 and e["tagline"] == "A fan edit." and e["tags"] == ["Fan Edit", "Holiday"]
    assert e["genres"] == {"add": ["Holiday", "Family"]} and e["studios"] == {"remove": ["Old Studio"]}
    assert (e["template"], e["source"], e["overlay"], e["score"]) == ("my-look", "imdb", False, 9.5)


def test_clearing_is_explicit_and_an_empty_box_means_no_change(ui):
    app, c, tok, root, srv = ui
    add(c, tok, clear_tagline="1", clear_overview="1", tags_mode="clear", genres="", genres_mode="replace")
    e = entries(root)[0]
    assert e["tagline"] == "" and e["overview"] == "" and e["tags"] == [] and "genres" not in e
    r = add(c, tok)                                                        # nothing filled in
    assert "note=mdbad" in r.headers["location"] or "note=mddup" in r.headers["location"]


@pytest.mark.parametrize("data", [{"critic_rating": "101"}, {"critic_rating": "5.5"}, {"critic_rating": "abc"}, {"tags": "a", "tags_mode": "rename"},
                                  {"template": "no-such"}, {"source": "bogus"}, {"title": "x" * 201}, {"tagline": "y" * 301},
                                  {"tags": ", ".join(str(i) for i in range(101))}, {"tags": "a\x07b"}])
def test_bad_input_is_refused_and_nothing_is_written(ui, data):
    app, c, tok, root, srv = ui
    r = add(c, tok, **data)
    assert "note=mdbad" in r.headers["location"] and not (root / "overrides.yaml").exists()


def test_a_written_entry_is_planned_and_applied_like_any_other(ui):
    app, c, tok, root, srv = ui
    srv.db["1"].update(Taglines=["Old."], TagItems=[], Genres=[], GenreItems=[], Studios=[])
    add(c, tok, critic_rating="80", tagline="New.", tags="Fan Edit")
    page = c.get("/metadata?s=emby").text
    assert "critic_rating" in page and "tagline" in page and "tags" in page and "Fan Edit" in page and "cannot lock" in page


def test_template_and_source_say_default_unless_overridden(ui, tmp_path):
    app, c, tok, root, srv = ui
    page = c.get("/item/1?s=emby").text
    assert '<select name="template"><option value="">Default</option>' in page and '<select name="source"><option value="">Default</option>' in page
    assert "(the usual one)" not in page and "(the template's)" not in page
    tsel = re.search(r'<select name="template">.*?</select>', page, re.S).group(0)
    assert tsel.count(">Default<") == 1                                          # not listed twice
    ssel = re.search(r'<select name="source">.*?</select>', page, re.S).group(0)
    assert ">IMDb<" in ssel and "MDBList combined score" not in ssel              # the default's own source is not offered again
    assert "means the title uses what it uses now" in page and "MDBList combined score" in page   # the (i) says what Default is
    # when the scheduled runs use another template, the first choice is still "Default" and the built-in one is named as such
    from laminator import schedule as schedmod
    (tmp_path / "t2" / "templates").mkdir(parents=True)
    (tmp_path / "t2" / "templates" / "my-look.yaml").write_text(
        re.sub(r"^sources:.*$", "sources: [imdb]", (Path(__file__).parent.parent / "src/laminator/assets/templates/default.yaml").read_text(), flags=re.M))
    app2 = create_app(dict(CFG), {"emby": MetaServer()}, tmp_path / "t2", "pw", FakeMdb(), sched=schedmod.Schedule(template="my-look"))
    c2 = TestClient(app2, follow_redirects=False); login(c2)
    page2 = c2.get("/item/1?s=emby").text
    assert '<select name="template"><option value="">Default</option><option value="default">Default (built-in)</option>' in page2
    assert '<select name="source"><option value="">Default</option>' in page2 and "scheduled runs use (My Look)" in page2


def test_the_planned_changes_can_be_searched_and_filtered_by_status_and_field(ui):
    app, c, tok, root, srv = ui
    write(root, HEAD + "  - match: {imdb: tt1}\n    rating: PG-13\n    sort_title: Elf (2003)\n  - match: {imdb: tt404}\n    rating: R\n")
    html = c.get("/metadata").text
    assert 'id="pf-reset"' in html and 'id="plan-filter"' in html and 'id="pf-q"' in html and 'id="pf-status"' in html and 'id="pf-field"' in html
    cards = re.findall(r'<section class="card2 plan" data-search="([^"]*)" data-status="([^"]*)" data-fields="([^"]*)"', html)
    assert len(cards) == 2
    elf = next(x for x in cards if "elf" in x[0])
    assert "pg-13" in elf[0] and "sort_title" in elf[0] and elf[1] == "change" and elf[2].split() == ["rating", "sort_title"]     # lower-case text to search, a status, its fields
    bad = next(x for x in cards if "elf" not in x[0])
    assert bad[1] == "error" and bad[2] == ""
    assert '<option value="rating">rating</option>' in html and '<option value="sort_title">sort_title</option>' in html          # the field list comes from the plan


# ---- a title that already has an override shows it filled in and edits it ---------------------------------------------------
def test_item_page_shows_the_existing_override_and_saving_replaces_it(ui):
    app, c, tok, root, srv = ui
    original = ("# my notes\noverrides:\n  - match: {imdb: tt9}   # another film\n    rating: R\n\n"
                "  # the Elf one\n  - match: {imdb: tt1}   # Elf\n    sort_title: \"Elf (2003)\"\n    tags: {add: [\"Fan Edit\", \"Xmas\"]}\n    score: 8.5\n\n"
                "  - match: {imdb: tt8}\n    rating: PG\n")
    write(root, original)
    page = c.get("/item/1?s=emby").text
    assert "Override for this item (in overrides.yaml)" in page and "already has an override" in page
    assert 'name="entry" value="1"' in page and 'value="Elf (2003)"' in page and 'value="Fan Edit, Xmas"' in page
    assert re.search(r'<option value="add" selected>', page) and 'name="score" maxlength="8" value="85"' in page
    r = c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), "entry": "1", "sort_title": "Elf", "score": "90"})
    assert "note=mdentry" in r.headers["location"]
    text = (root / "overrides.yaml").read_text()
    assert text.startswith("# my notes\noverrides:\n  - match: {imdb: tt9}   # another film\n    rating: R\n\n  # the Elf one\n")
    assert text.endswith("\n\n  - match: {imdb: tt8}\n    rating: PG\n") and "# Elf" in text   # neighbours, comments and the entry's own comment kept
    es = entries(root)
    assert len(es) == 3 and es[1] == {"match": {"imdb": "tt1"}, "sort_title": "Elf", "score": 9.0} and es[0]["rating"] == "R" and es[2]["rating"] == "PG"
    assert "note=mddup" in add(c, tok, rating="R").headers["location"]                    # still no second entry through the add path


def test_editing_refuses_a_stale_or_wrong_entry_and_changes_nothing(ui):
    app, c, tok, root, srv = ui
    write(root, "overrides:\n  - match: {imdb: tt1}\n    rating: R\n  - match: {imdb: tt9}\n    rating: PG\n")
    before = (root / "overrides.yaml").read_text()
    for entry in ("1", "7", "-1", "x"):                                                   # not this item's entry / out of range / junk
        r = c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), "entry": entry, "rating": "PG-13"})
        assert "note=mdbad" in r.headers["location"], entry
    assert (root / "overrides.yaml").read_text() == before


def test_editing_keeps_the_server_limit_and_the_match(ui):
    app, c, tok, root, srv = ui
    write(root, 'overrides:\n  - match: {library: Movies, title: "Elf", year: 2003}\n    server: emby\n    rating: R\n')
    c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), "entry": "0", "rating": "PG-13", "no_overlay": "1"})
    assert entries(root) == [{"match": {"library": "Movies", "title": "Elf", "year": 2003}, "server": "emby", "rating": "PG-13", "overlay": False}]
    assert "checked" in re.search(r'<input type="checkbox" name="no_overlay"[^>]*>', c.get("/item/1?s=emby").text).group(0)


def test_poster_score_is_typed_on_the_100_scale_like_the_critic_rating(ui):
    app, c, tok, root, srv = ui
    add(c, tok, score="66")
    assert entries(root)[0]["score"] == 6.6                                    # the file keeps the 0-10 score the poster code reads
    page = c.get("/item/1?s=emby").text
    assert 'name="score" maxlength="8" value="66"' in page and "0-100, or short text" in page   # and the form shows it as 66 again
    write(root, "")
    add(c, tok, score="82.5"); assert entries(root)[0]["score"] == 8.25
    assert 'value="82.5"' in c.get("/item/1?s=emby").text
    write(root, "")
    add(c, tok, score="A+"); assert entries(root)[0]["score"] == "A+" and 'name="score" maxlength="8" value="A+"' in c.get("/item/1?s=emby").text
    write(root, "")
    add(c, tok, score="0"); assert entries(root)[0]["score"] == 0.0 and 'name="score" maxlength="8" value="0"' in c.get("/item/1?s=emby").text


def test_one_title_the_server_refuses_does_not_stop_the_rest(ui):
    import httpx
    app, c, tok, root, srv = ui
    srv.items_.append(NS(id="2", name="Gremlins", type="Movie", year=1984, provider_ids={"imdb": "tt2"}, has_poster=True, poster_tag="t2"))
    srv.db["2"] = {"Id": "2", "Name": "Gremlins", "OfficialRating": "PG", "ForcedSortName": "Gremlins", "OriginalTitle": "", "Overview": "", "CriticRating": None, "LockedFields": []}
    real = srv.post_item
    def post_item(i, body):
        if i == "1":
            raise httpx.HTTPStatusError("bad", request=httpx.Request("POST", "http://x/Items/1"), response=httpx.Response(400))
        real(i, body)
    srv.post_item = post_item
    write(root, HEAD + "  - match: {imdb: tt1}\n    rating: PG-13\n  - match: {imdb: tt2}\n    rating: R\n")
    r = c.post("/metadata/apply", data={"srv": "emby", "token": tok()})
    log = finished(app, c, r.headers["location"])
    assert "the server refused the update (HTTP 400)" in log and "Gremlins" in log
    assert srv.db["2"]["OfficialRating"] == "R" and srv.db["1"]["OfficialRating"] == "PG"      # the second title was still written
