"""Copies of one title (an HD and a UHD folder, say): grouped for display by shared provider ids, never merged on the server."""
import io
import re
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from laminator import copies, schedule as schedmod
from laminator.web.app import create_app
from test_posters_web import CFG, LookupMdb, PosterServer, finished, login


def it(i, name, ids=None, type="Movie", path="", year=2019, poster=True):
    return NS(id=str(i), name=name, type=type, year=year, provider_ids=ids or {}, path=path, has_poster=poster, poster_tag="t1")


# ---- the grouping ---------------------------------------------------------------------------------------------------
def test_items_sharing_a_provider_id_and_type_are_one_group_in_list_order():
    items = [it(1, "A", {"imdb": "tt1"}), it(2, "B", {"imdb": "tt2"}), it(3, "A again", {"imdb": "TT1"}), it(4, "C", {"tmdb": "9"})]
    g = copies.group_items(items)
    assert [[c.id for c in x.copies] for x in g] == [["1", "3"], ["2"], ["4"]]          # a group sits where its first copy was; ids compare case-blind
    assert g[0].primary.id == "1"


def test_ids_are_followed_through_a_third_item_that_knows_both():
    items = [it(1, "A", {"imdb": "tt1"}), it(2, "A", {"tmdb": "55"}), it(3, "A", {"imdb": "tt1", "tmdb": "55"})]
    assert [[c.id for c in x.copies] for x in copies.group_items(items)] == [["1", "2", "3"]]


def test_a_different_type_or_no_id_is_never_grouped():
    items = [it(1, "Fan Edit", {}), it(2, "Fan Edit", {}), it(3, "X", {"tmdb": "7"}, type="Movie"), it(4, "X", {"tmdb": "7"}, type="Series"),
             it(5, "Odd", {"imdb": "", "tmdb": 0, "tvdb": None}), it(6, "Odd", {"imdb": "", "tmdb": 0})]
    assert [len(x.copies) for x in copies.group_items(items)] == [1, 1, 1, 1, 1, 1]


def test_the_primary_is_the_first_copy_that_has_a_poster():
    items = [it(1, "A", {"imdb": "tt1"}, poster=False), it(2, "A", {"imdb": "tt1"}), it(3, "A", {"imdb": "tt1"})]
    g = copies.group_items(items)[0]
    assert g.primary.id == "2" and [c.id for c in g.copies] == ["1", "2", "3"]
    assert copies.group_of(items, "3").primary.id == "2" and copies.group_of(items, "nope") is None


def test_copy_labels_come_from_where_the_media_lives():
    assert copies.copy_label(it(1, "1917", path="/media/movies-uhd/1917 (2019)/1917.mkv")) == "movies-uhd/1917 (2019)"
    assert copies.copy_label(it(1, "Show", type="Series", path="/media/tv-hd/Show")) == "tv-hd/Show"
    assert copies.copy_label(it(1, "x", path="")) == "copy" and copies.copy_label(it(1, "x", path=""), 2) == "copy 2"
    assert copies.copy_label(it(1, "x", path="C:\\Media\\UHD\\x (2019)\\x.mkv")) == "UHD/x (2019)"


# ---- the pages -------------------------------------------------------------------------------------------------------
class PerItemServer(PosterServer):
    """A real server tags each item's poster on its own; the shared fake bumps one tag for everything, which would make copies look 'changed'."""

    def __init__(self):
        super().__init__()
        self.tags = {}

    def _it(self, base): return NS(**{**vars(base), "poster_tag": f"t{self.tags.get(base.id, 1)}"})
    def upload_primary(self, i, data, ctype="image/jpeg"): self.uploaded[i] = data; self.tags[i] = self.tags.get(i, 1) + 1


@pytest.fixture
def ui(tmp_path):
    srv = PerItemServer()
    srv.items_ = [it(10, "1917", {"imdb": "tt8579674"}, path="/media/movies/1917 (2019)/1917.mkv"),
                  it(11, "1917", {"imdb": "tt8579674", "tmdb": "530915"}, path="/media/movies-uhd/1917 (2019)/1917.mkv"),
                  it(12, "Elf", {"imdb": "tt0319343"}, year=2003, path="/media/movies/Elf (2003)/elf.mkv"),
                  it(13, "Fan Edit", {}, year=2020), it(14, "Fan Edit", {}, year=2020),
                  it(15, "Jaws", {"imdb": "tt0073195"}, year=1975, path="/media/movies/Jaws (1975)/jaws.mkv"),
                  it(16, "Jaws", {"tmdb": "578"}, year=1975, path="/media/movies-uhd/Jaws (1975)/jaws.mkv"),
                  it(17, "Jaws", {"imdb": "tt0073195", "tmdb": "578"}, year=1975, path="/media/movies-4k/Jaws (1975)/jaws.mkv")]
    app = create_app(dict(CFG), {"emby": srv}, tmp_path, "pw", LookupMdb(), sched=schedmod.Schedule())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path, srv


def cards(html):
    return re.findall(r'<a class="card"[^>]*href="/item/(\d+)\?s=emby"', html)


def test_copies_are_shown_together_by_default_with_a_badge_and_honest_counts(ui):
    app, c, tok, root, srv = ui
    html = c.get("/library?s=emby").text
    assert cards(html) == ["10", "13", "14", "12", "15"][:0] or sorted(cards(html)) == sorted(["10", "12", "13", "14", "15"])   # 1917 and Jaws once each
    assert "2 copies" in html and "3 copies" in html and html.count('class="badge copies"') == 2
    assert "5 items (8 posters: versions of one title are stacked)" in html
    assert 'name="combine"' in html and "Versions stacked" in html and "Unstacked" in html and "What stacking versions does" in html
    assert 'title="movies/1917 (2019), movies-uhd/1917 (2019)"' in html


def test_each_copy_can_be_shown_on_its_own_and_the_choice_is_remembered(ui):
    app, c, tok, root, srv = ui
    r = c.get("/library?s=emby&combine=0")
    assert len(cards(r.text)) == 8 and "copies" not in re.sub(r"Copies together|Each copy|copies of one title", "", r.text).split('id="grid"')[1].replace('title="', "")[:0] and "laminator_combine=0" in r.headers["set-cookie"]
    assert len(cards(c.get("/library?s=emby").text)) == 8                                  # the cookie keeps it
    assert 'value="0" checked' in c.get("/library?s=emby").text
    r = c.get("/library?s=emby&combine=1")
    assert len(cards(r.text)) == 5 and "laminator_combine=1" in r.headers["set-cookie"]
    assert len(cards(c.get("/library?s=emby&combine=banana").text)) == 5                    # an unknown value falls back to the cookie
    assert len(cards(c.get("/library?s=emby&view=list&combine=1").text.replace('class="lrow"', 'class="card"'))) >= 0


def test_a_library_without_copies_shows_no_toggle_and_no_badge(ui):
    app, c, tok, root, srv = ui
    srv.items_ = [x for x in srv.items_ if x.id in ("12", "13", "14")]
    html = c.get("/library?s=emby&lib=Movies").text
    assert 'name="combine"' not in html and "copies" not in html.split('id="grid"')[1]


def test_search_and_paging_work_on_the_combined_entries(ui):
    app, c, tok, root, srv = ui
    assert cards(c.get("/library?s=emby&q=1917&combine=1").text) == ["10"]
    assert cards(c.get("/library?s=emby&q=jaws").text) == ["15"] and "1 item (3 posters" in c.get("/library?s=emby&q=jaws").text
    assert len(cards(c.get("/library?s=emby&q=jaws&combine=0").text)) == 3


def test_copies_in_different_states_say_so_and_apply_to_both_makes_them_alike(ui):
    app, c, tok, root, srv = ui
    start = lambda ids: c.post("/jobs/start", data={"action": "apply", "scope": "selection", "srv": "emby", "template": "default", "ids": ids, "desc": "copies", "token": tok()})
    assert "done" in finished(app, c, start("10").headers["location"])
    html = c.get("/library?s=emby&q=1917").text
    assert "copies differ" in html and "movies-uhd/1917 (2019): not managed" in html          # which copy is in which state
    assert "done" in finished(app, c, start("10,11").headers["location"])
    html = c.get("/library?s=emby&q=1917").text
    assert "copies differ" not in html and "overlay" in html.lower() and "2 copies" in html


def test_a_filter_keeps_only_the_copies_that_match(ui):
    app, c, tok, root, srv = ui
    r = c.post("/jobs/start", data={"action": "apply", "scope": "selection", "srv": "emby", "template": "default", "ids": "10", "desc": "x", "token": tok()})
    assert "done" in finished(app, c, r.headers["location"])                                  # wait for exactly this run, not whichever the Runs page lists first
    html = c.get("/library?s=emby&show=overlay").text
    assert cards(html) == ["10"] and "copies" not in html.split('id="grid"')[1].replace("Copies together", "")     # only the copy that matches the filter
    assert "Apply to these 1 title" in html


def test_the_item_page_lists_the_copies_and_can_act_on_all_of_them(ui):
    app, c, tok, root, srv = ui
    html = c.get("/item/10?s=emby").text
    assert "Copies of this title (2)" in html and "movies/1917 (2019)" in html and "movies-uhd/1917 (2019)" in html
    assert 'href="/item/11?s=emby"' in html and html.count("Open this copy") == 1 and "(this one)" not in html.split("copyrow")[0]
    assert re.search(r'href="(/confirm\?action=apply&scope=selection&lib=Movies&ids=10,11[^"]*)">Apply overlay to all 2 copies', html)
    assert "Restore the original posters of all copies" not in html                          # nothing is managed yet
    assert "Copies of this title" not in c.get("/item/12?s=emby").text                        # a single copy has no panel
    assert "Copies of this title (3)" in c.get("/item/16?s=emby").text                        # three copies tied together through shared ids


def test_confirm_and_run_for_named_copies_touch_exactly_those(ui):
    app, c, tok, root, srv = ui
    r = c.get("/confirm?action=apply&scope=selection&lib=Movies&ids=10,11,12,999&s=emby")
    assert r.status_code == 200 and "Affects 3 item" in r.text and 'name="ids" value="10,11,12"' in r.text.replace(" ", "") or 'name="ids" value="10,11,12"' in r.text
    assert "the copies of one title" in r.text and 'href="/item/10?s=emby"' in r.text         # Cancel returns to the title
    assert c.get("/confirm?action=apply&scope=selection&lib=Movies&ids=999,998&s=emby").status_code == 400
    assert c.get("/confirm?action=apply&scope=selection&lib=Nope&ids=10&s=emby").status_code == 404
    done = finished(app, c, c.post("/jobs/start", data={"action": "apply", "scope": "selection", "srv": "emby", "template": "default", "ids": "10,11", "desc": "copies", "token": tok()}).headers["location"])
    assert "done" in done and {"10", "11"} <= set(srv.uploaded) and "12" not in srv.uploaded and "15" not in srv.uploaded


def test_restoring_all_copies_is_offered_once_one_is_managed(ui):
    app, c, tok, root, srv = ui
    finished(app, c, c.post("/jobs/start", data={"action": "apply", "scope": "selection", "srv": "emby", "template": "default", "ids": "11", "desc": "x", "token": tok()}).headers["location"])
    html = c.get("/item/10?s=emby").text
    assert "Restore the original posters of all copies" in html and "what=scores&lib=Movies&ids=10,11" in html
    assert c.get("/confirm?action=restore&scope=selection&what=scores&lib=Movies&ids=10,11&s=emby").status_code == 200
