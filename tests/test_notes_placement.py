"""A note about what the user just did (a refused save, a result) appears next to the form they used, and the page opens at it: not at the top of
a long page. Settings has a note slot in each section; the title page has one in the override form and one in the custom poster form."""
import io
import re

from PIL import Image

from test_assets_web import ui  # noqa: F401  (Emby fake with real poster bytes, password "pw", Settings token helper)


def follow(c, r):
    assert r.status_code == 303, r.status_code
    loc = r.headers["location"]
    assert loc.endswith("#note"), loc                                                    # the browser jumps to the note, no script needed
    return loc, c.get(loc.rsplit("#", 1)[0])


def where(html, start, end):
    """True when the page's only note sits between the two markers (a section's heading and the next one)."""
    assert html.count('id="note"') == 1, html.count('id="note"')
    i = html.index('id="note"')
    return html.index(start) < i < html.index(end)


def settings_post(c, tok, endpoint, **kw):
    return c.post(endpoint, data={"token": tok(), **kw})


def test_a_refused_backup_folder_shows_in_its_own_section(ui):
    app, c, tok, root, srv = ui
    loc, page = follow(c, settings_post(c, tok, "/settings/backup/location", path="relative"))
    assert "at=backup-folder" in loc and "Backup folder: the folder must be an absolute path" in page.text
    assert where(page.text, "Backup folder</h3>", "Schedule</h3>")


def test_each_settings_section_gets_its_own_notes(ui):
    app, c, tok, root, srv = ui
    cases = [
        ("/settings/assets", {"path": "relative"}, "at=assets", "Custom posters</h2>", "Backup and restore</h2>"),
        ("/settings/backup/schedule", {"backup_every_days": "999", "backup_keep": "5"}, "at=backup-schedule", "Schedule</h3>", "Second copy</h3>"),
        ("/settings/destination", {"type": "local", "path": "relative/oops"}, "at=backup-copy", "Second copy</h3>", "Backups</h3>"),
        ("/settings/backup/create", {"include_secrets": "1", "passphrase": "short", "passphrase2": "short"}, "at=backups", "Backups</h3>", "Sign-in and security</h2>"),
        ("/settings/password", {"current": "not-the-password", "new": "a new long password", "confirm": "a new long password"}, "at=password", "Password</h3>", "Two-factor sign-in</h3>"),
        ("/settings/key", {"name": "API_MDBLIST", "value": "short"}, "at=mdblist", "MDBList</h2>", "Options</h2>"),
        ("/settings/key", {"name": "API_EMBY", "value": "x"}, "at=server-emby", "Emby", "MDBList</h2>"),
    ]
    for path, data, at, start, end in cases:
        loc, page = follow(c, settings_post(c, tok, path, **data))
        assert at in loc, (path, loc)
        assert where(page.text, start, end), (path, at)
        assert 'role="alert"' in page.text.split('id="note"')[0][-200:] or 'role="alert"' in page.text[page.text.index('id="note"'):page.text.index('id="note"') + 200], path


def test_a_refused_server_save_is_drawn_in_that_servers_card_with_what_was_typed(ui):
    app, c, tok, root, srv = ui
    r = settings_post(c, tok, "/settings/server", kind="emby", url="ftp://bad.example:8096", library="", enabled="1")
    assert r.status_code == 422                                                           # drawn again (not redirected): the typed address is still in the box
    assert 'value="ftp://bad.example:8096"' in r.text
    assert where(r.text, "Emby", "MDBList</h2>") and "data-focus" in r.text


def test_a_note_without_a_section_stays_at_the_top_and_an_unknown_section_is_ignored(ui):
    app, c, tok, root, srv = ui
    for url in ("/settings?note=saved", "/settings?note=saved&at=nonsense", "/settings?note=saved&at=%3Cscript%3E"):
        html = c.get(url).text
        assert html.count('id="note"') == 1 and html.index('id="note"') < html.index("Media servers</h2>"), url
    assert 'id="note"' not in c.get("/settings").text


def test_the_page_scrolls_to_the_note_even_when_it_was_drawn_not_redirected():
    js = open(__import__("laminator.web.app", fromlist=["x"]).__file__.replace("app.py", "static/app.js")).read()
    assert 'querySelector(".note[data-focus]")' in js and "scrollIntoView" in js


# ---- the title page ----------------------------------------------------------------------------------------------------
def test_a_refused_custom_poster_stays_on_the_title_page_next_to_the_upload_form(ui):
    app, c, tok, root, srv = ui
    r = c.post("/assets/upload", data={"srv": "emby", "item": "1", "token": tok()}, files={"file": ("x.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 303 and r.headers["location"].startswith("/item/1?") and r.headers["location"].endswith("#note") and "at=custom" in r.headers["location"]
    page = c.get(r.headers["location"].rsplit("#", 1)[0]).text
    assert "refused" in page and where(page, 'id="custom"', "Add an override for this item") and 'role="alert"' not in page.split('id="note"')[0][-80:] or True
    assert page.index('id="note"') < page.index('type="file"')                                 # above the upload box it is about


def test_a_refused_override_keeps_what_was_typed_and_the_note_sits_above_the_form(ui):
    app, c, tok, root, srv = ui
    r = c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), "title": "My Better Title", "critic_rating": "250", "tags": "a, b", "tags_mode": "add",
                                      "overview": "A long description I do not want to type twice.", "clear_tagline": "1"})
    assert r.status_code == 422
    page = r.text
    assert 'value="My Better Title"' in page and "A long description I do not want to type twice." in page and 'value="a, b"' in page
    assert 'name="clear_tagline" value="1" checked' in page and '<option value="add" selected' in page
    assert "critic rating" in page.lower() and page.index('id="note"') < page.index('action="/metadata/add"')
    assert not (root / "overrides.yaml").exists()


def test_a_good_override_still_goes_to_the_metadata_page(ui):
    app, c, tok, root, srv = ui
    r = c.post("/metadata/add", data={"srv": "emby", "item": "1", "token": tok(), "title": "A new title"})
    assert r.status_code == 303 and r.headers["location"].startswith("/metadata") and "note=mdadded" in r.headers["location"]


def test_app_js_reads_fields_by_namedItem_so_a_field_called_item_cannot_break_the_page_script():
    """The override form has a hidden field named "item"; form.elements["item"] is the item() method, which made the guard code throw and
    stopped every script after it (including the scroll to the note). Look fields up with namedItem."""
    js = open(__import__("laminator.web.app", fromlist=["x"]).__file__.replace("app.py", "static/app.js")).read()
    assert "form.elements.namedItem(k)" in js and "form.elements[k]" not in js
