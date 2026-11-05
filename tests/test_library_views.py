import re
from types import SimpleNamespace as NS

import pytest
from fastapi.testclient import TestClient

from laminator.web.app import bucket, create_app, sort_key
from test_web import FakeServer, FakeMdb, login

NAMES = ["Émile", "Elf", "Fargo", "10 Things", "Zodiac", "Alien", "Zulu", "_Odd"] + [f"{c}{i} Movie" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for i in range(3)]


@pytest.fixture
def lib(tmp_path):
    srv = FakeServer()
    srv.items_ = [NS(id=str(n), name=name, type="Movie", year=2000 + n % 20, provider_ids={}, has_poster=True, poster_tag="t") for n, name in enumerate(NAMES, 1)]
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, srv, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    return c


def test_sorting_folds_accents_and_puts_digits_and_symbols_first():
    assert [bucket(n) for n in ("Émile", "Elf", "10 Things", "_Odd", "zulu")] == ["E", "E", "#", "#", "Z"]
    assert sorted(["Zulu", "Émile", "Elf", "10 Things", "_Odd", "Alien"], key=sort_key) == ["10 Things", "_Odd", "Alien", "Elf", "Émile", "Zulu"]


def test_three_views_render_and_the_choice_is_remembered(lib):
    c = lib
    grid = c.get("/library?view=posters")
    assert 'class="grid "' in grid.text and "lrows" not in grid.text and "laminator_view=posters" in grid.headers["set-cookie"]
    assert 'class="grid compact"' in c.get("/library?view=compact").text
    r = c.get("/library?view=list")
    assert 'class="lrows"' in r.text and 'class="lrow"' in r.text.replace('id="letter-num" ', "").replace(re.search(r' id="letter-\w+"', r.text).group(0), "")
    again = c.get("/library")                                                     # no view in the URL: the cookie decides
    assert 'class="lrows"' in again.text and 'checked' in re.search(r'<input type="radio" name="view" value="list"[^>]*>', again.text).group(0)
    assert 'class="grid ' in c.get("/library?view=bogus").text or 'lrows' in c.get("/library?view=bogus").text   # a junk value is ignored
    assert "set-cookie" not in c.get("/library?view=%3Cscript%3E").headers


def test_the_rail_appears_for_big_libraries_and_jumps_to_the_right_page_and_letter(lib):
    c = lib
    page = c.get("/library?view=list").text                                       # list: 100 per page, 86 items -> one page
    rail = re.findall(r'<a href="(/library\?[^"]+)">(\S)</a>', page)
    assert [l for _, l in rail][:3] == ["#", "A", "B"] and "Q" in [l for _, l in rail]
    href = dict((l, h) for h, l in rail)["Z"]
    assert "page_no=1" in href and href.endswith("#letter-Z") and 'id="letter-Z"' in page and 'id="letter-num"' in page
    p = c.get("/library?view=posters").text                                       # 48 per page: later letters live on page 2
    rail2 = dict((l, h) for h, l in re.findall(r'<a href="(/library\?[^"]+)">(\S)</a>', p))
    assert "page_no=1" in rail2["A"] and "page_no=2" in rail2["Z"] and 'id="letter-A"' in p
    p2 = c.get(rail2["Z"].split("#")[0]).text
    assert p2.count('id="letter-Z"') == 1 and p2.count('id="letter-A"') == 1       # scroll mode shows everything up to that chunk; each letter has one anchor
    n = c.get("/library?view=posters&per=25").text                                 # fixed pages: a letter lives only on its own page
    r3 = dict((l, h) for h, l in re.findall(r'<a href="(/library\?[^"]+)">(\S)</a>', n))
    z = c.get(r3["Z"].split("#")[0]).text
    assert z.count('id="letter-Z"') == 1 and 'id="letter-A"' not in z


def test_no_rail_for_a_tiny_library_or_a_single_letter(tmp_path):
    srv = FakeServer()                                                            # 2 items
    c = TestClient(create_app({"server": {"kind": "emby", "url": "http://x"}}, srv, tmp_path, "pw", FakeMdb()), follow_redirects=False); login(c)
    assert "alpharail" not in c.get("/library").text
    assert "alpharail" not in c.get("/library?q=zzz").text


def test_no_label_for_the_criticrating_write_but_overridden_metadata_still_shows(tmp_path):
    from laminator import schedule as schedmod
    from laminator import store as st
    from test_posters_web import CFG, LookupMdb, PosterServer
    sd = tmp_path / "servers" / "emby"; sd.mkdir(parents=True)
    s = st.Store(sd / "state.db")
    s.meta_put("1", "Elf", "critic_rating", None, 73, True)
    s.meta_put("2", "x", "rating", "PG", "PG-13", True)
    app = create_app(dict(CFG), {"emby": PosterServer()}, tmp_path, "pw", LookupMdb(), sched=schedmod.Schedule())
    c = TestClient(app, follow_redirects=False); login(c)
    page = c.get("/library?s=emby").text
    assert "CriticRating" not in page and "critic rating" not in page.lower()
    assert page.count('>metadata</span>') == 2 and "What the labels on each title mean" in page      # the legend mentions it once, the card once


def test_per_page_choice_scroll_loading_and_the_cookie(lib):
    c = lib
    first = c.get("/library?view=posters")                                         # default: scroll mode, one chunk and a sentinel for the next
    assert first.text.count('class="card"') + first.text.count('class="card" id=') >= 1 and "sentinel" in first.text and "pager" not in first.text
    assert 'hx-trigger="loadmore once"' in first.text and "page_no=2&more=1" in first.text.replace("&amp;", "&")
    part = c.get("/library?view=posters&page_no=2&more=1", headers={"HX-Request": "true"}).text
    assert "<html" not in part and "<p class=\"muted\">" not in part.split("sentinel")[0][:5]      # just the next cards, no page around them
    assert 'id="letter-A"' not in part and part.count('class="card"') + part.count('class="card" id=') > 0
    assert "sentinel" not in part                                                  # 86 titles: the second chunk is the last one
    sized = c.get("/library?view=posters&per=25")                                  # a fixed size: pages with Next, no sentinel
    assert "sentinel" not in sized.text and "Next" in sized.text and "page 1 of 4" in sized.text
    assert "laminator_per=25" in sized.headers["set-cookie"]
    again = c.get("/library?view=posters")                                         # no per in the URL: the cookie decides
    assert "page 1 of 4" in again.text and "sentinel" not in again.text
    assert "set-cookie" not in c.get("/library?per=%3Cscript%3E").headers          # a junk value is ignored and not stored
    assert 'name="per"' in again.text and 'value="500"' in again.text
    c.get("/library?per=scroll")
    assert "sentinel" in c.get("/library?view=posters").text


def test_the_first_steps_note_shows_until_something_is_applied_or_a_schedule_is_on(lib):
    c = lib
    page = c.get("/library").text
    assert "Nothing has been applied yet, and nothing runs on its own" in page and "Apply to this library" in page


def test_grid_posters_keep_their_2_to_3_shape_whatever_size_attributes_the_images_carry():
    from pathlib import Path
    css = (Path(__file__).parent.parent / "src/laminator/web/static/style.css").read_text()
    rule = next(line for line in css.splitlines() if line.startswith(".card img { width:100%"))
    assert "height:auto" in rule and "aspect-ratio:2/3" in rule      # a height attribute on the <img> would otherwise stretch every poster


def test_menu_styles_are_scoped_to_the_top_menu_so_the_az_rail_is_never_stretched_by_them():
    import re
    from pathlib import Path
    css = (Path(__file__).parent.parent / "src/laminator/web/static/style.css").read_text()
    assert not re.search(r"(^|[\s,{};])nav\s*(\{|a\b|::|:)", css, re.M)      # a bare `nav` rule also matched the rail, which is a <nav> too
    base = (Path(__file__).parent.parent / "src/laminator/web/templates/base.html").read_text()
    assert 'class="mainnav"' in base and 'id="menu-toggle"' in base         # the top menu has its own class and the hamburger toggle


def _titles(html):
    return re.findall(r'<strong>([^<]+)</strong> <span class="muted(?: yr)?">', html)


@pytest.fixture
def filt(tmp_path):
    import json
    from laminator import mdblist as mdbmod, store as st
    mk = lambda i, name, ids=None, poster=True, year=2000, rating=None, aspect=0.67: NS(id=i, name=name, type="Movie", year=year, provider_ids=ids or {}, has_poster=poster, poster_tag="t1", path=f"/m/{name}/{name}.mkv", rating=rating, aspect=aspect)
    srv = FakeServer()
    srv.items_ = [mk("1", "Alpha", {"imdb": "tt1"}, year=1975, rating="PG-13"), mk("2", "Bravo", {"imdb": "tt2"}, year=1985, rating="PG-13"),
                  mk("3", "Charlie", {"imdb": "tt3"}, year=1995, rating="R"), mk("4", "Delta fan edit", year=2005),
                  mk("5", "Echo", {"imdb": "tt5"}, year=2015, rating="PG", aspect=1.78), mk("6", "Foxtrot", {"imdb": "tt6"}, poster=False, year=2025, rating="R")]
    cache = tmp_path / "cache" / "mdblist"; cache.mkdir(parents=True)
    for name, score in (("tt1", 80), ("tt2", 0), ("tt5", 70)):                       # Charlie (tt3) was never looked up; Bravo got a 0 = no score
        (cache / mdbmod.cache_name("imdb", "movie", name)).write_text(json.dumps({"score": score, "ratings": []}))
    (cache / mdbmod.cache_name("imdb", "movie", "tt3")).write_text(json.dumps({"score": 0, "ratings": [{"source": "imdb", "score": 65}, {"source": "tmdb", "score": 90}]}))   # no combined score, but IMDb 6.5
    sd = tmp_path / "servers" / "emby"; sd.mkdir(parents=True)
    s = st.Store(sd / "state.db")
    s.put_original("5", "Echo", str(sd / "5.img"), "sha", "t1"); s.set_applied("5", "h", "7.0", "t1", "sha2")   # an overlay is on Echo, whose image is a 16:9 frame
    s.put_original("2", "Bravo", str(sd / "2.img"), "sha", "t1"); s.set_applied("2", "h", "0.0", "t1", "sha3")   # an old badge reading 0.0 on Bravo
    s.meta_put("1", "Alpha", "rating", "PG", "PG-13", True)                                                   # Alpha's rating was written
    s.db.close()
    (tmp_path / "overrides.yaml").write_text("overrides:\n  - match: {title: Delta fan edit}\n    score: 7.5\n  - match: {imdb: tt3}\n    rating: PG-13\n")
    (tmp_path / "assets" / "Bravo").mkdir(parents=True)
    Image = __import__("PIL.Image", fromlist=["Image"]); Image.new("RGB", (200, 300)).save(tmp_path / "assets" / "Bravo" / "poster.jpg")
    app = create_app({"server": {"kind": "emby", "url": "http://x"}}, srv, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    return c


@pytest.mark.parametrize("show, expected", [
    ("no_mdb", ["Bravo", "Charlie", "Delta fan edit", "Foxtrot"]),                  # a 0, a lookup without a combined score, no ids at all, and no ids
    ("has_mdb", ["Alpha", "Echo"]),
    ("my_score", ["Delta fan edit"]),
    ("in_file", ["Charlie", "Delta fan edit"]),
    ("overlay", ["Bravo", "Echo"]),
    ("thumb", ["Echo"]),                                                           # Emby's image for it is landscape: a video frame, not a poster
    ("zero", ["Bravo"]),                                                           # a 0.0 badge from before a 0 meant "no score"
    ("not_managed", ["Alpha", "Charlie", "Delta fan edit", "Foxtrot"]),
    ("meta_written", ["Alpha"]),
    ("custom", ["Bravo"]),
    ("no_poster", ["Foxtrot"]),
    ("", ["Alpha", "Bravo", "Charlie", "Delta fan edit", "Echo", "Foxtrot"]),
    ("nonsense", ["Alpha", "Bravo", "Charlie", "Delta fan edit", "Echo", "Foxtrot"]),     # an unknown filter is ignored
])
def test_the_library_filters_find_the_right_titles(filt, show, expected):
    c = filt
    r = c.get(f"/library?view=list&show={show}")
    assert r.status_code == 200 and _titles(r.text) == expected


def test_the_filter_combines_with_search_keeps_going_while_scrolling_and_can_be_cleared(filt):
    c = filt
    assert _titles(c.get("/library?view=list&show=no_mdb&q=a").text) == ["Bravo", "Charlie", "Delta fan edit"]      # search narrows the filter
    page = c.get("/library?view=list&show=no_mdb").text
    assert "filter: <strong>No MDBList score</strong>" in page and 'reset filters' in page and "show=" not in re.search(r'href="(/library\?[^"]+)">reset filters', page).group(1)
    assert 'value="no_mdb" selected' in page and "<optgroup" in page


def test_a_filtered_list_keeps_loading_more_with_the_filter_in_the_next_request(lib):
    c = lib                                                                          # 86 titles, none with a provider id: all "no MDBList score"
    first = c.get("/library?view=posters&show=no_mdb").text.replace("&amp;", "&")
    assert "sentinel" in first and "show=no_mdb" in re.search(r'class="sentinel"[^>]*hx-get="([^"]+)"', first).group(1)
    part = c.get("/library?view=posters&show=no_mdb&page_no=2&more=1", headers={"HX-Request": "true"}).text
    assert part.count('class="card"') == 86 - 48 and "sentinel" not in part
    assert c.get("/library?view=posters&show=my_score").text.count('class="card"') == 0   # nothing matches: an empty list, not an error


@pytest.mark.parametrize("query, expected", [
    ("smin=7.5", ["Alpha", "Delta fan edit"]),                                     # the manual 7.5 override counts as Delta's score
    ("smax=7", ["Echo"]),
    ("smin=7&smax=8", ["Alpha", "Echo", "Delta fan edit"]),
    ("smin=9", []),
    ("ssrc=imdb&smin=6", ["Charlie"]),                                             # one provider's saved score, when there is no combined one
    ("ssrc=tmdb&smin=8.5", ["Charlie"]),
    ("ymin=1990&ymax=2010", ["Charlie", "Delta fan edit"]),
    ("ymin=2015", ["Echo", "Foxtrot"]),
    ("ymax=1985", ["Alpha", "Bravo"]),
    ("crate=R", ["Charlie", "Foxtrot"]),
    ("crate=PG-13", ["Alpha", "Bravo"]),
    ("crate=__none__", ["Delta fan edit"]),
    ("show=no_mdb&crate=R", ["Charlie", "Foxtrot"]),                               # filters combine
    ("show=has_mdb&smin=7.5&ymax=1980", ["Alpha"]),
    ("smin=abc&ymin=&crate=", ["Alpha", "Bravo", "Charlie", "Delta fan edit", "Echo", "Foxtrot"]),    # junk and empty values are ignored
    ("smin=-5&smax=99", ["Alpha", "Delta fan edit", "Echo"]),                      # clamped to 0-10; titles with no score are left out
])
def test_score_year_and_content_rating_filters(filt, query, expected):
    r = filt.get(f"/library?view=list&{query}")
    assert r.status_code == 200 and sorted(_titles(r.text)) == sorted(expected)


def test_the_active_filters_are_described_and_cleared_together_and_the_wording_is_neutral(filt):
    page = filt.get("/library?view=list&show=no_mdb&smin=7.5&ymax=1990&crate=R&ssrc=imdb").text
    assert "score at least 7.5 (IMDb)" in page and "year up to 1990" in page and "content rating R" in page and "No MDBList score" in page
    assert "<details class=\"morefilters\" open>" in page                                      # opened because a score, year or rating is set
    assert 'PG-13 (2)' in page and 'R (2)' in page and 'Not set (1)' in page                      # the ratings present, with counts
    clear = re.search(r'href="(/library\?[^"]+)">reset filters', page).group(1)
    assert not any(k in clear for k in ("show=", "smin", "ymax", "crate"))
    low = page.lower()
    assert "my score" not in low and "using my" not in low and "manual score override" in low


def test_scrolling_carries_every_filter_in_its_next_request(lib):
    first = lib.get("/library?view=posters&crate=__none__&ymin=1990").text.replace("&amp;", "&")
    nxt = re.search(r'class="sentinel"[^>]*hx-get="([^"]+)"', first)
    assert nxt is None or ("crate=__none__" in nxt.group(1))                                      # (these fake titles have no year: nothing to scroll)
    first = lib.get("/library?view=posters&crate=__none__").text.replace("&amp;", "&")
    assert "crate=__none__" in re.search(r'class="sentinel"[^>]*hx-get="([^"]+)"', first).group(1)


def test_reset_filters_button_appears_only_when_something_is_filtered_and_clears_everything(filt):
    c = filt
    plain = c.get("/library?view=list&s=emby&lib=Movies").text
    assert "Reset filters" not in plain                                                           # nothing to reset
    for query in ("show=no_mdb", "q=al", "smin=7", "ymin=1990", "crate=R"):
        page = c.get(f"/library?view=list&s=emby&lib=Movies&per=50&{query}").text
        btn = re.search(r'<a class="btn plain resetbtn" href="(/library\?[^"]+)"', page)
        assert btn, query
        href = btn.group(1).replace("&amp;", "&")
        assert "lib=Movies" in href and "view=list" in href and "per=50" in href                  # where you are stays
        assert not any(k in href for k in ("q=al", "show=", "smin", "smax", "ymin", "ymax", "crate"))   # everything else is cleared, the search too
        assert len(_titles(c.get(href).text)) == 6 and "Reset filters" not in c.get(href).text


def test_the_reset_button_is_refreshed_with_the_list_so_it_appears_while_filtering(filt):
    full = filt.get("/library?view=list&s=emby&lib=Movies").text
    assert full.count('id="resetslot"') == 1 and "Reset filters" not in full                       # one slot in the controls, empty when nothing is filtered
    part = filt.get("/library?view=list&s=emby&lib=Movies&q=al", headers={"HX-Request": "true"}).text
    assert 'id="resetslot" hx-swap-oob="true"' in part and "Reset filters" in part                 # the list refresh also carries the button
    assert 'hx-swap-oob' not in filt.get("/library?view=list&s=emby&lib=Movies&q=al").text         # a normal page does not repeat the slot
