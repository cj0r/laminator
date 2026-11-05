from pathlib import Path
import re

import pytest
import yaml
from fastapi.testclient import TestClient

from laminator import assets, schedule as schedmod
from laminator.web.app import create_app
from test_posters_web import CFG, LookupMdb, PosterServer, login


@pytest.fixture
def ui(tmp_path):
    app = create_app(dict(CFG), {"emby": PosterServer()}, tmp_path, "pw", LookupMdb(), sched=schedmod.Schedule())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/templates?s=emby").text).group(1)
    return app, c, tok, tmp_path


LOOK = {"s": "emby", "name": "default", "gh": "0.2", "mo": "0.5", "sz": "0.05", "mx": "0.04", "my": "0.03", "em": "0", "font": "Anton-Regular.ttf", "source": "imdb"}


def test_only_default_is_listed_and_shown_capitalised_with_the_combined_score_selected(ui):
    app, c, tok, root = ui
    page = c.get("/templates?s=emby").text
    assert page.count('name="name"') == 1 and ">Default</option>" in page and "bebas-embolden" not in page.lower()
    assert re.search(r'<option value="aggregate" selected>MDBList combined score</option>', page)         # the default source
    for label in ("IMDb", "TMDb", "Trakt", "Letterboxd", "Rotten Tomatoes (critics)", "Metacritic (critics)"):
        assert label in page


def test_saving_a_named_template_stores_the_look_and_chosen_source_and_lists_it(ui):
    app, c, tok, root = ui
    r = c.post("/templates/save", data={**LOOK, "new_name": "My Big Score!", "token": tok()})
    assert r.status_code == 303 and "note=saved" in r.headers["location"] and "name=my-big-score" in r.headers["location"]
    saved = yaml.safe_load((root / "templates" / "my-big-score.yaml").read_text())
    assert saved["sources"] == ["imdb", "aggregate"] and saved["gradient"]["height"] == 0.2 and saved["score"]["size"] == 0.05
    assert saved["score"]["font"] == "Anton-Regular.ttf"                                                   # a file name, not a machine path
    page = c.get("/templates?s=emby&name=my-big-score").text
    assert ">My Big Score</option>" in page and ">Default</option>" in page and "Delete My Big Score" in page
    assert re.search(r'<option value="imdb" selected>', page)                                              # reopening shows the saved source
    assert "My Big Score" in c.get("/settings").text                                                       # usable for scheduled runs


def test_the_combined_score_alone_has_no_fallback_and_the_preview_follows_the_chosen_source(ui):
    app, c, tok, root = ui
    c.post("/templates/save", data={**LOOK, "source": "aggregate", "new_name": "plain", "token": tok()})
    assert yaml.safe_load((root / "templates" / "plain.yaml").read_text())["sources"] == ["aggregate"]
    panel = c.get("/templates/panel?s=emby&name=default&source=tmdb").text
    assert "tmdb" in panel and "aggregate" in panel                                                        # the YAML shown reflects the choice


def test_bad_names_the_builtin_name_and_junk_sources_are_refused_or_ignored(ui):
    app, c, tok, root = ui
    for bad in ("!!!", "Default", "default"):
        r = c.post("/templates/save", data={**LOOK, "new_name": bad, "token": tok()})
        assert r.status_code == 422 and 'class="note bad"' in r.text, bad
    assert not (root / "templates").exists() or not list((root / "templates").glob("*.yaml"))
    r = c.post("/templates/save", data={**LOOK, "source": "../../etc/passwd", "new_name": "x1", "token": tok()})
    assert yaml.safe_load((root / "templates" / "x1.yaml").read_text())["sources"] == ["aggregate", "imdb", "tmdb", "letterboxd"]   # unknown source ignored
    r = c.post("/templates/save", data={**LOOK, "new_name": "../../evil", "token": tok()})
    assert "name=evil" in r.headers["location"] and (root / "templates" / "evil.yaml").exists() and not (root.parent / "evil.yaml").exists()


def test_deleting_only_works_for_your_own_templates_and_not_the_one_in_use(ui):
    app, c, tok, root = ui
    c.post("/templates/save", data={**LOOK, "new_name": "mine", "token": tok()})
    assert "nodel" in c.post("/templates/delete", data={"name": "default", "token": tok()}).headers["location"]   # nothing of your own to remove yet
    assert "nodel" in c.post("/templates/delete", data={"name": "../x", "token": tok()}).headers["location"]
    app.state.scheduler.sched.template = "mine"
    assert "inuse" in c.post("/templates/delete", data={"name": "mine", "token": tok()}).headers["location"]
    assert (root / "templates" / "mine.yaml").exists()
    app.state.scheduler.sched.template = "default"
    assert "deleted" in c.post("/templates/delete", data={"name": "mine", "token": tok()}).headers["location"]
    assert not (root / "templates" / "mine.yaml").exists()


def test_csrf_and_read_only_apply(ui, tmp_path):
    app, c, tok, root = ui
    assert c.post("/templates/save", data={**LOOK, "new_name": "z", "token": ""}).status_code == 403
    assert c.post("/templates/delete", data={"name": "z", "token": ""}).status_code == 403
    assert c.post("/templates/save", data={**LOOK, "new_name": "z", "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert not (root / "templates").exists()


GOOD = Path(__file__).parent.parent.joinpath("src/laminator/assets/templates/default.yaml").read_text()


def post_yaml(c, tok, **kw):
    return c.post("/templates/yaml", data={"s": "emby", "name": "default", "item": "1", "token": tok(), **kw})


def test_the_menu_and_page_say_templates_and_offer_an_edit_yaml_button(ui):
    app, c, tok, root = ui
    page = c.get("/templates?s=emby").text
    assert ">Templates</a>" in page and "<h1>Templates" in page and "<title>Templates - laminator</title>" in page
    assert "/templates/yaml?" in c.get("/templates/panel?s=emby&name=default&item=1").text


def test_the_editor_starts_from_the_current_sliders_and_a_valid_check_shows_a_test_drawing(ui):
    app, c, tok, root = ui
    r = c.get("/templates/yaml?s=emby&name=default&gh=0.2&source=imdb&item=1")
    assert r.status_code == 200 and "height: 0.2" in r.text and "imdb" in r.text and "Anton-Regular.ttf" in r.text
    ok = post_yaml(c, tok, text=GOOD, action="check", new_name="")
    assert ok.status_code == 200 and "valid and draws correctly" in ok.text and 'src="data:image/jpeg;base64,' in ok.text
    assert not (root / "templates").exists()                                                        # checking writes nothing


def test_invalid_yaml_is_refused_with_reasons_and_the_text_stays_in_the_box(ui):
    app, c, tok, root = ui
    cases = ((GOOD.replace("size: 0.0433", "size: 9"), "score.size"), ("sources: [aggregate", "not valid YAML"),
             (GOOD + "evil: 1\n", "unknown setting"), (GOOD.replace("Anton-Regular.ttf", "/etc/passwd"), "just the file name"),
             (GOOD.replace("[0, 0, 0]", "[0, 0, 999]"), "gradient.color"), ("- just\n- a list\n", "not a list"))
    for text, reason in cases:
        r = post_yaml(c, tok, text=text, action="save", new_name="bad")
        assert r.status_code == 422 and reason in r.text and "Not saved" in r.text, reason
        if "evil" in text:
            assert "evil: 1" in r.text and "evil" in r.text
    assert not (root / "templates").exists()
    r = post_yaml(c, tok, text=GOOD.replace("size: 0.0433", "size: 9"), action="check")
    assert "size: 9" in r.text                                                                      # what you typed is still there


def test_saving_writes_exactly_what_was_checked_and_it_becomes_a_usable_template(ui):
    app, c, tok, root = ui
    text = "# my fan edit look\n" + GOOD.replace("height: 0.12", "height: 0.2")
    r = post_yaml(c, tok, text=text, action="save", new_name="Fan Edit!")
    assert r.status_code == 303 and "name=fan-edit" in r.headers["location"] and "note=saved" in r.headers["location"]
    assert (root / "templates" / "fan-edit.yaml").read_text() == text                               # comments kept
    assert ">Fan Edit</option>" in c.get("/templates?s=emby&name=fan-edit").text
    assert "height: 0.2" in c.get("/templates/panel?s=emby&name=fan-edit").text


def test_names_and_guards_for_the_yaml_editor(ui):
    app, c, tok, root = ui
    for bad in ("!!!", "default", "Default"):
        r = post_yaml(c, tok, text=GOOD, action="save", new_name=bad)
        assert r.status_code == 422, bad
    assert post_yaml(c, tok, text=GOOD, action="save", new_name="").status_code == 303        # an empty name updates the template being edited
    assert (root / "templates" / "default.yaml").exists()
    import shutil; shutil.rmtree(root / "templates")
    assert post_yaml(c, lambda: "", text=GOOD, action="save", new_name="x").status_code == 403
    r = c.post("/templates/yaml", data={"s": "emby", "name": "default", "text": GOOD, "action": "save", "new_name": "x", "token": tok()}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403 and not (root / "templates").exists()
    assert c.get("/templates/yaml?s=emby&name=nope").status_code == 404


def test_save_without_a_name_updates_the_open_template_and_a_taken_name_is_an_error_that_keeps_the_settings(ui):
    app, c, tok, root = ui
    r = c.post("/templates/save", data={**LOOK, "new_name": "", "sz": "0.07", "token": tok()})     # no name: Default is saved as your own copy
    assert r.status_code == 303 and "note=updated" in r.headers["location"]
    assert yaml.safe_load((root / "templates" / "default.yaml").read_text())["score"]["size"] == 0.07
    assert yaml.safe_load(assets.template_file("default", root).read_text())["score"]["size"] == 0.07   # and it is the one used from now on
    assert "Reset to built-in" in c.get("/templates?s=emby&name=default").text
    c.post("/templates/save", data={**LOOK, "new_name": "mine", "token": tok()})
    r = c.post("/templates/save", data={**LOOK, "name": "mine", "new_name": "", "mx": "0.1", "token": tok()})   # update a template of your own
    assert "note=updated" in r.headers["location"] and yaml.safe_load((root / "templates" / "mine.yaml").read_text())["score"]["margin_x"] == 0.1
    r = c.post("/templates/save", data={**LOOK, "new_name": "Mine", "sz": "0.099", "score": "9.9", "token": tok()})   # a name that is taken
    assert r.status_code == 422 and "already exists" in r.text
    assert 'value="0.099"' in r.text and 'value="9.9"' in r.text and 'value="Mine"' in r.text            # nothing the user set was reset
    assert yaml.safe_load((root / "templates" / "mine.yaml").read_text())["score"]["size"] != 0.099
    r = c.post("/templates/delete", data={"name": "default", "token": tok()})                    # Default can be put back to the built-in look
    assert "note=reset" in r.headers["location"] and not (root / "templates" / "default.yaml").exists()
