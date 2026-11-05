"""Emby AND Jellyfin side by side: separate state, explicit targets, address-change safety, settings."""
import os
import re
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import yaml
from fastapi.testclient import TestClient

from laminator import actions, cli, config, secrets_store, store
from laminator import schedule as schedmod
from laminator import meta as metamod
from laminator.config import ConfigError
from laminator.web.app import create_app
from test_web import FakeServer, FakeMdb, login

TWO = {"servers": {"emby": {"url": "http://emby.lan:8096"}, "jellyfin": {"url": "http://jelly.lan:8096"}}}


class JellyServer(FakeServer):
    def __init__(self):
        super().__init__()
        self.items_ = [NS(id="abc123", name="Jelly Movie", type="Movie", year=2010, provider_ids={"imdb": "tt9"}, has_poster=True, poster_tag="j1")]

    def ping(self): return "Fake Jellyfin 10.9"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for n in ("API_EMBY", "API_JELLYFIN", "API_MDBLIST"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setattr(secrets_store, "_UNDER", {})


@pytest.fixture
def two(tmp_path):
    app = create_app(dict(TWO), {"emby": FakeServer(), "jellyfin": JellyServer()}, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path


# ---- config -------------------------------------------------------------------------------------------------
def test_servers_accepts_old_and_new_shapes_and_only_one_per_kind():
    assert list(config.servers({"server": {"kind": "jellyfin", "url": "u"}})) == ["jellyfin"]
    assert list(config.servers(TWO)) == ["emby", "jellyfin"]
    assert config.enabled_servers({"servers": {"emby": {"url": "a"}, "jellyfin": {"url": "b", "enabled": False}}}) == ["emby"]
    with pytest.raises(ConfigError):
        config.servers({"servers": {"plex": {"url": "a"}}})
    with pytest.raises(ConfigError, match="not set"):
        config.load_server({"servers": {"emby": {}}}, "emby")


def test_settings_overlay_the_config_file_without_rewriting_it(tmp_path):
    cfgfile = tmp_path / "c.yaml"
    cfgfile.write_text(f"data_dir: {tmp_path}\n# my comment\nserver:\n  kind: emby\n  url: http://old:8096\ncritic_rating: false\n")
    before = cfgfile.read_text()
    config.save_settings(tmp_path, {"servers": {"emby": {"url": "http://new:8096", "enabled": True}, "jellyfin": {"url": "http://j:8096"}},
                                    "critic_rating": True, "schedule": {"changed_every_minutes": 0}})
    cfg = config.load_config(cfgfile)
    assert cfg["servers"]["emby"]["url"] == "http://new:8096" and cfg["servers"]["jellyfin"]["url"] == "http://j:8096"
    assert cfg["critic_rating"] is True and cfg["schedule"]["changed_every_minutes"] == 0 and "server" not in cfg
    assert cfgfile.read_text() == before                                    # the hand-edited file is never touched


def test_a_missing_config_file_is_fine_for_the_ui_but_not_for_other_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("LAMINATOR_DATA", str(tmp_path))
    assert config.load_config(tmp_path / "nope.yaml", required=False) == {}
    with pytest.raises(ConfigError, match="not found"):
        config.load_config(tmp_path / "nope.yaml")


# ---- CLI: never act on both servers by accident ---------------------------------------------------------------
def args(**kw):
    return NS(server=kw.get("server"))


def test_with_two_servers_a_command_must_name_one():
    with pytest.raises(ConfigError, match="both configured"):
        cli.chosen_servers(TWO, args())
    assert cli.chosen_servers(TWO, args(server="jellyfin")) == ["jellyfin"]
    assert cli.chosen_servers(TWO, args(server="all")) == ["emby", "jellyfin"]
    assert cli.chosen_servers(TWO, args(), default_all=True) == ["emby", "jellyfin"]     # maintain: whole-system by nature
    assert cli.chosen_servers({"server": {"kind": "emby", "url": "u"}}, args()) == ["emby"]   # one server: no flag needed
    with pytest.raises(ConfigError, match="no jellyfin"):
        cli.chosen_servers({"server": {"kind": "emby", "url": "u"}}, args(server="jellyfin"))
    off = {"servers": {"emby": {"url": "a"}, "jellyfin": {"url": "b", "enabled": False}}}
    with pytest.raises(ConfigError, match="switched off"):
        cli.chosen_servers(off, args(server="jellyfin"))
    with pytest.raises(ConfigError, match="no media server"):
        cli.chosen_servers({}, args())


def test_cli_refuses_an_ambiguous_write_before_touching_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    f = tmp_path / "c.yaml"
    f.write_text(f"data_dir: {tmp_path}/d\nservers:\n  emby: {{url: 'http://127.0.0.1:9'}}\n  jellyfin: {{url: 'http://127.0.0.1:9'}}\n")
    monkeypatch.setenv("API_EMBY", "e" * 16); monkeypatch.setenv("API_JELLYFIN", "j" * 16); monkeypatch.setenv("API_MDBLIST", "m" * 16)
    for cmd in (["apply", "--all"], ["restore", "--all"], ["scan"], ["meta", "diff"]):
        assert cli.main(["-c", str(f)] + cmd) == 2
        assert "both configured" in capsys.readouterr().err
    assert not (tmp_path / "d" / "servers" / "emby" / "state.db").exists()                 # nothing was even opened


def test_each_server_has_its_own_state_folder_and_legacy_state_goes_to_emby(tmp_path, monkeypatch, capsys):
    from test_migrate import legacy
    legacy(tmp_path)                                                       # an old single-server data folder
    monkeypatch.setenv("API_EMBY", "e" * 16); monkeypatch.setenv("API_JELLYFIN", "j" * 16)
    cfg = {"data_dir": str(tmp_path), **TWO}
    ws_j = actions.open_workspace(cfg, need_mdblist=False, kind="jellyfin")
    assert (tmp_path / "state.db").exists()                                # a Jellyfin run must NOT claim the old Emby state
    ws_e = actions.open_workspace(cfg, need_mdblist=False, kind="emby")
    assert not (tmp_path / "state.db").exists() and "moved existing state" in capsys.readouterr().err
    assert ws_e.data == tmp_path / "servers" / "emby" and ws_j.data == tmp_path / "servers" / "jellyfin"
    assert len(ws_e.store.all()) == 2 and ws_j.store.all() == []           # Emby keeps its records; Jellyfin starts empty
    assert ws_e.root == ws_j.root == tmp_path


def test_overrides_can_name_a_server(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    rating: R\n    server: jellyfin\n  - match: {imdb: tt2}\n    score: 9\n")
    entries = metamod.load_overrides(f)
    assert [e["match"]["imdb"] for e in metamod.for_server(entries, "emby")] == ["tt2"]
    assert [e["match"]["imdb"] for e in metamod.for_server(entries, "jellyfin")] == ["tt1", "tt2"]
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    rating: R\n    server: plex\n")
    with pytest.raises(metamod.OverrideError):
        metamod.load_overrides(f)


# ---- web: two servers at once --------------------------------------------------------------------------------------
def test_switching_servers_shows_each_servers_own_items(two):
    app, c, tok, data = two
    html = c.get("/library").text
    assert "Elf" in html and "Jelly Movie" not in html and "Emby" in html and "Jellyfin" in html          # header switcher
    r = c.get("/switch?s=jellyfin&next=/library")
    assert r.status_code == 303 and "laminator_srv=jellyfin" in r.headers["set-cookie"]
    assert "Jelly Movie" in c.get("/library").text and "Elf" not in c.get("/library").text
    assert "Elf" in c.get("/library?s=emby").text                                       # an explicit ?s= wins over the cookie
    assert c.get("/item/abc123?s=jellyfin").status_code == 200 and c.get("/item/abc123?s=emby").status_code == 404
    assert c.get("/img/abc123/current?s=jellyfin").status_code == 200
    assert c.get("/switch?s=evil&next=//evil.example/x").headers["location"] == "/library"       # no open redirect
    assert c.get("/switch?s=emby&next=https://evil.example").headers["location"] == "/library"


def test_a_write_must_name_a_connected_server_and_lands_on_that_servers_state(two, monkeypatch):
    app, c, tok, data = two
    seen = []

    def fake_apply(ws, tpl, sig, overrides_path, required=False, item=None, critic=False, only_changed=False):
        seen.append((ws.scfg.kind, ws.data, ws.root, type(ws.srv).__name__))
        yield actions.Event("apply", "x", "y")

    monkeypatch.setattr(actions, "apply_posters", fake_apply)
    t = re.search(r'name="token" value="([0-9a-f]+)"', c.get("/confirm?action=apply&scope=library&lib=Movies").text).group(1)
    base = {"action": "apply", "scope": "library", "lib": "Movies", "template": "default", "token": t, "changed": "1"}
    assert c.post("/jobs/start", data=base).status_code == 400                          # no server named: refused
    assert c.post("/jobs/start", data={**base, "srv": "plex"}).status_code == 400
    r = c.post("/jobs/start", data={**base, "srv": "jellyfin"})
    assert r.status_code == 303
    import time
    for _ in range(100):
        if seen: break
        time.sleep(0.02)
    kind, d, root, cls = seen[0]
    assert kind == "jellyfin" and d == data / "servers" / "jellyfin" and root == data and cls == "JellyServer"
    assert "Jellyfin: apply library: Movies" in c.get("/jobs").text


def test_keys_are_per_server_and_shown_for_both(two):
    app, c, tok, data = two
    page = c.get("/settings").text
    assert "API_MDBLIST" in page and page.count('name="api_key"') == 2      # each server card has its key field in its one form
    r = c.post("/settings/key", data={"name": "API_JELLYFIN", "value": "JELLYKEY-0123456789abcdef", "token": tok()})
    assert "note=" in r.headers["location"] and os.environ["API_JELLYFIN"].startswith("JELLYKEY")
    assert "API_EMBY" not in os.environ and secrets_store.read(data) == {"API_JELLYFIN": "JELLYKEY-0123456789abcdef"}
    assert "JELLYKEY-0123456789abcdef" not in c.get("/settings").text


# ---- web: settings for addresses and options -------------------------------------------------------------------------------
@pytest.fixture
def fresh(tmp_path):
    """No servers configured at all: the first-run state."""
    built = []
    app = create_app({}, {}, tmp_path, "pw", None, sched=schedmod.Schedule(), rebuild=lambda: built.append(1) or ({"emby": FakeServer()}, None))
    c = TestClient(app, follow_redirects=False); login(c)
    tok = lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)
    return app, c, tok, tmp_path, built


def test_first_run_can_add_a_server_entirely_from_the_ui(fresh):
    app, c, tok, data, built = fresh
    assert c.get("/library").headers["location"] == "/settings?note=noserver"
    page = c.get("/settings").text
    assert "Emby" in page and "Jellyfin" in page and "not set up" in page
    r = c.post("/settings/server", data={"kind": "emby", "url": "http://10.0.0.5:8096/", "library": "Movies", "enabled": "1", "token": tok()})
    assert r.status_code == 303 and built
    saved = yaml.safe_load((data / "settings.yaml").read_text())
    assert saved["servers"]["emby"] == {"url": "http://10.0.0.5:8096", "library": "Movies", "enabled": True}     # normalised
    assert "10.0.0.5:8096" in c.get("/settings").text and "Movies" in c.get("/settings").text
    assert c.get("/library").status_code == 200                                          # connected now (via the rebuild)


def test_bad_addresses_and_enabling_without_one_are_rejected(fresh):
    app, c, tok, data, built = fresh
    for bad in ("ftp://host", "http://user:pw@host:8096", "http://host/?x=1", "javascript:alert(1)", "http://" + "a" * 300, "host:99999"):
        r = c.post("/settings/server", data={"kind": "emby", "url": bad, "library": "Movies", "enabled": "1", "token": tok()})
        assert r.status_code == 422 and 'class="note bad"' in r.text, bad              # the page comes back with a message...
        assert 'value="Movies"' in r.text, bad                                             # ...and everything typed is still in the fields
    r = c.post("/settings/server", data={"kind": "emby", "url": "ftp://host", "library": "Movies", "enabled": "1", "token": tok()})
    assert 'value="ftp://host"' in r.text
    r = c.post("/settings/server", data={"kind": "jellyfin", "url": "", "library": "Kids", "enabled": "1", "token": tok()})
    assert r.status_code == 422 and 'value="Kids"' in r.text
    assert c.post("/settings/server", data={"kind": "plex", "url": "http://x", "token": tok()}).status_code == 400
    assert not (data / "settings.yaml").exists()


def seed_state(data, kind="emby"):
    d = config.server_dir({"data_dir": str(data)}, kind)
    (d / "originals").mkdir(exist_ok=True); (d / "originals" / "8.img").write_bytes(b"ORIG")
    st = store.Store(d / "state.db"); st.put_original("8", "Elf", str(d / "originals" / "8.img"), "sha", "tag"); st.db.close()
    return d


def test_changing_an_address_with_saved_data_asks_and_archive_keeps_everything(fresh):
    app, c, tok, data, built = fresh
    c.post("/settings/server", data={"kind": "emby", "url": "http://old.lan:8096", "enabled": "1", "token": tok()})
    d = seed_state(data)
    form = {"kind": "emby", "url": "http://new.lan:8096", "enabled": "1", "token": tok()}
    r = c.post("/settings/server", data=form)
    assert r.status_code == 200 and "Is this the same Emby server?" in r.text and "1 saved record" in r.text      # asked, nothing saved yet
    assert yaml.safe_load((data / "settings.yaml").read_text())["servers"]["emby"]["url"] == "http://old.lan:8096"
    assert (d / "state.db").exists()
    r = c.post("/settings/server", data={**form, "decision": "keep"})
    assert r.status_code == 303 and (d / "state.db").exists()
    assert yaml.safe_load((data / "settings.yaml").read_text())["servers"]["emby"]["url"] == "http://new.lan:8096"
    r = c.post("/settings/server", data={**form, "url": "http://third.lan:8096", "decision": "archive", "token": tok()})
    assert r.status_code == 303 and not (d / "state.db").exists() and not (d / "originals").exists()
    archive = next(d.glob("archive-*"))
    assert (archive / "state.db").exists() and (archive / "originals" / "8.img").read_bytes() == b"ORIG"       # moved, not deleted
    assert store.state_counts(d) == (0, 0)


def test_the_other_servers_state_is_never_affected(fresh):
    app, c, tok, data, built = fresh
    c.post("/settings/server", data={"kind": "emby", "url": "http://e.lan:8096", "enabled": "1", "token": tok()})
    c.post("/settings/server", data={"kind": "jellyfin", "url": "http://j.lan:8096", "enabled": "1", "token": tok()})
    de, dj = seed_state(data, "emby"), seed_state(data, "jellyfin")
    c.post("/settings/server", data={"kind": "jellyfin", "url": "http://other.lan:8096", "enabled": "1", "decision": "archive", "token": tok()})
    assert (de / "state.db").exists() and not (dj / "state.db").exists()


def test_options_validate_save_and_reset(fresh):
    app, c, tok, data, built = fresh
    ok = {"critic": "1", "changed_every_minutes": "60", "maint_on": "1", "maint_day": "15", "maint_time": "04:30",
          "sched_template": "default", "token": tok()}
    r = c.post("/settings/general", data=ok)
    assert "note=genok" in r.headers["location"]
    saved = yaml.safe_load((data / "settings.yaml").read_text())
    assert saved["critic_rating"] is True and saved["schedule"] == {"changed_every_minutes": 60, "template": "default",
                                                                  "maintenance": {"day": 15, "time": "04:30"},
                                                                  "backup": {"every_days": 7, "keep": 5}}
    page = c.get("/settings").text
    assert 'value="15"' in page and 'value="04:30"' in page and "Back to config file" in page
    assert app.state.scheduler.sched.changed_every_minutes == 60 and app.state.scheduler.sched.maintenance_day == 15   # live, no restart
    for bad in ({"changed_every_minutes": "3"}, {"changed_every_minutes": "x"}, {"maint_day": "31"}, {"maint_time": "25:99"},
                {"sched_template": "../../etc/passwd"}):
        r = c.post("/settings/general", data={**ok, **bad, "token": tok()})
        assert "note=bad" in r.headers["location"], bad
    assert yaml.safe_load((data / "settings.yaml").read_text())["schedule"]["changed_every_minutes"] == 60        # unchanged by the bad ones
    c.post("/settings/general", data={**{k: v for k, v in ok.items() if k != "maint_on"}, "token": tok()})        # maintenance switched off
    assert app.state.scheduler.sched.maintenance_day is None
    r = c.post("/settings/reset", data={"section": "general", "token": tok()})
    assert "note=reset" in r.headers["location"] and "schedule" not in yaml.safe_load((data / "settings.yaml").read_text())
    assert c.post("/settings/reset", data={"section": "../etc", "token": tok()}).status_code == 400


def test_settings_writes_need_csrf_origin_and_a_password(fresh, tmp_path):
    app, c, tok, data, built = fresh
    body = {"kind": "emby", "url": "http://e.lan:8096", "enabled": "1"}
    assert c.post("/settings/server", data={**body, "token": ""}).status_code == 403
    assert c.post("/settings/server", data={**body, "token": tok()}, headers={"origin": "http://evil.example"}).status_code == 403
    assert c.post("/settings/general", data={"changed_every_minutes": "30", "token": "x"}).status_code == 403
    assert c.post("/settings/reset", data={"section": "general", "token": "x"}).status_code == 403
    assert not (data / "settings.yaml").exists()
    (tmp_path / "ro").mkdir()
    ro = TestClient(create_app({}, {}, tmp_path / "ro", None), follow_redirects=False)
    assert ro.post("/settings/server", data={**body, "token": "x"}).status_code == 403
    assert ro.post("/settings/general", data={"token": "x"}).status_code == 403


def test_an_unreachable_server_shows_a_message_instead_of_crashing_the_page(tmp_path):
    import httpx

    class Down(FakeServer):
        def libraries(self): raise httpx.ConnectError("no route")
        def items(self, lib=None, limit=None): raise httpx.ConnectError("no route")

    class Rejected(FakeServer):
        def libraries(self): raise httpx.HTTPStatusError("x", request=None, response=NS(status_code=401))

    app = create_app(dict(TWO), {"emby": Down(), "jellyfin": Rejected()}, tmp_path, "pw")
    c = TestClient(app, follow_redirects=False); login(c)
    html = c.get("/library?s=emby").text
    assert c.get("/library?s=emby").status_code == 200 and "could not reach the server (ConnectError)" in html
    assert "HTTP 401" in c.get("/library?s=jellyfin").text and "is the API key right" in c.get("/library?s=jellyfin").text
    assert c.get("/status").status_code == 200 and c.get("/settings").status_code == 200       # the rest of the UI is unaffected


def test_one_save_button_per_server_also_stores_its_key(two):
    app, c, tok, data = two
    page = c.get("/settings").text
    assert page.count(">Save Emby<") == 1 and page.count(">Save Jellyfin<") == 1 and "Save and test" in page      # the last one is MDBList
    r = c.post("/settings/server", data={"kind": "jellyfin", "url": "http://j:8096", "library": "", "enabled": "1",
                                         "api_key": "JELLYKEY-0123456789abcdef", "token": tok()})
    assert r.status_code == 303 and secrets_store.read(data) == {"API_JELLYFIN": "JELLYKEY-0123456789abcdef"}
    assert "JELLYKEY-0123456789abcdef" not in r.headers["location"] and "JELLYKEY-0123456789abcdef" not in c.get("/settings").text
    r = c.post("/settings/server", data={"kind": "jellyfin", "url": "http://j:8096", "api_key": "bad key!", "token": tok()})
    assert r.status_code == 422 and 'class="note bad"' in r.text                                        # refused, page comes back as typed
    r = c.post("/settings/server", data={"kind": "jellyfin", "url": "http://j:8096", "api_key": "", "token": tok()})      # blank keeps it
    assert secrets_store.read(data) == {"API_JELLYFIN": "JELLYKEY-0123456789abcdef"}


def test_a_bad_address_does_not_lose_the_typed_key_and_a_bare_address_gets_its_scheme(fresh):
    app, c, tok, data, built = fresh
    r = c.post("/settings/server", data={"kind": "emby", "url": "ftp://nope", "api_key": "EMBYKEY-0123456789abcdef", "enabled": "1", "token": tok()})
    assert r.status_code == 422 and "The API key was saved." in r.text and "EMBYKEY-0123456789abcdef" not in r.text
    assert secrets_store.read(data) == {"API_EMBY": "EMBYKEY-0123456789abcdef"}
    for typed, saved in (("10.0.0.5:8096", "http://10.0.0.5:8096"), ("home.local", "http://home.local"), ("nas", "http://nas"),
                         ("localhost:8096", "http://localhost:8096"), ("emby.example.com", "https://emby.example.com"),
                         ("emby.example.com/emby", "https://emby.example.com/emby"), ("http://emby.example.com", "http://emby.example.com"),
                         ("https://home.local:8920", "https://home.local:8920")):
        r = c.post("/settings/server", data={"kind": "emby", "url": typed, "enabled": "1", "token": tok()})
        assert r.status_code == 303, typed
        assert config.load_settings(data)["servers"]["emby"]["url"] == saved, typed


# ---- servers need not hold the same titles ---------------------------------------------------------------------------------------
def _full(self, i):
    it = next(x for x in self.items_ if x.id == i)
    return {"Id": it.id, "Name": it.name, "ForcedSortName": it.name, "LockedFields": []}


class MetaEmby(FakeServer):
    get_full = _full


class MetaJelly(JellyServer):
    get_full = _full


class _NoMeta:
    def meta_get(self, i): return {}


ENTRIES = [{"match": {"imdb": "tt1"}, "sort_title": "Elf (2003)"},                    # only on Emby
           {"match": {"imdb": "tt9"}, "sort_title": "Jelly"},                         # only on Jellyfin
           {"match": {"imdb": "tt404"}, "sort_title": "Nowhere"},                     # on neither: a real problem
           {"match": {"imdb": "tt1"}, "server": "jellyfin", "sort_title": "Limited"}]  # limited to Jellyfin but not there: a real problem


def test_an_entry_for_a_title_only_on_the_other_server_is_absent_not_an_error():
    emby, jelly = MetaEmby(), MetaJelly()
    libs = lambda s: {"Movies": s.items()}
    plans = metamod.plan_all(jelly, _NoMeta(), ENTRIES, None, "jellyfin", elsewhere=lambda: {"emby": libs(emby)})
    kinds = [("absent:" + p.absent) if p.absent else ("error" if p.error else "ok") for p in plans]
    assert kinds == ["absent:emby", "ok", "error", "error"] and plans[1].name == "Jelly Movie"
    plans = metamod.plan_all(emby, _NoMeta(), ENTRIES, None, "emby", elsewhere=lambda: {"jellyfin": libs(jelly)})
    assert [("absent:" + p.absent) if p.absent else ("error" if p.error else "ok") for p in plans] == ["ok", "absent:jellyfin", "error"]
    plans = metamod.plan_all(jelly, _NoMeta(), ENTRIES, None, "jellyfin")                # nothing known about other servers: as before
    assert [bool(p.error) for p in plans] == [True, False, True, True] and not any(p.absent for p in plans)


def test_the_metadata_page_lists_absent_titles_apart_from_problems(tmp_path):
    app = create_app(dict(TWO), {"emby": MetaEmby(), "jellyfin": MetaJelly()}, tmp_path, "pw", FakeMdb())
    c = TestClient(app, follow_redirects=False); login(c); data = tmp_path
    (data / "overrides.yaml").write_text("overrides:\n  - match: {imdb: tt1}\n    sort_title: Elf (2003)\n  - match: {imdb: tt9}\n    sort_title: Jelly\n  - match: {imdb: tt404}\n    sort_title: X\n")
    html = c.get("/metadata?s=jellyfin").text
    assert "1 override for titles that are not on Jellyfin" in html and "only in Emby" in html and "Jelly Movie" in html
    assert "no item matches {&#39;imdb&#39;: &#39;tt404&#39;}" in html or "no item matches" in html
    assert html.count("will change") >= 1 and "{&#39;imdb&#39;: &#39;tt1&#39;}" not in html.split("Planned changes")[1].split("not on Jellyfin")[0]   # tt1 is not a problem card
    emby = c.get("/metadata?s=emby").text
    assert "1 override for titles that are not on Emby" in emby and "only in Jellyfin" in emby


def test_the_command_line_says_not_on_this_server_and_does_not_count_it_as_a_failure(capsys):
    plans = [metamod.ItemPlan("", "{'imdb': 'tt1'}", absent="emby"), metamod.ItemPlan("", "{'imdb': 'tt404'}", error="no item matches")]
    assert cli._print_plan(plans, "[jellyfin] ") == 0
    out = capsys.readouterr().out
    assert "not on this server (it is on emby); nothing to do" in out and out.count("ERROR") == 1


def test_asking_about_one_title_does_not_list_entries_that_match_nothing():
    jelly = MetaJelly()
    plans = metamod.plan_all(jelly, _NoMeta(), ENTRIES, "Jelly Movie", "jellyfin", elsewhere=lambda: {"emby": {"Movies": MetaEmby().items()}})
    assert [p.name for p in plans] == ["Jelly Movie"]


def test_settings_can_include_or_leave_out_a_server_from_scheduled_runs(two):
    app, c, tok, data = two
    jf = data / "servers" / "jellyfin"
    page = c.get("/settings").text
    assert page.count("Include in scheduled runs") == 2 and "Not included yet" in page                 # no saved state: both new here
    base = {"kind": "jellyfin", "url": "http://j:8096", "enabled": "1", "scheduled_set": "1"}
    assert c.post("/settings/server", data={**base, "scheduled": "1", "token": tok()}).status_code == 303
    assert schedmod.load(jf)["scheduled"] is True and schedmod.scheduled_on(jf)
    c.post("/settings/server", data={**base, "token": tok()})                                           # box cleared
    assert schedmod.load(jf)["scheduled"] is False
    c.post("/settings/server", data={"kind": "jellyfin", "url": "http://j:8096", "enabled": "1", "token": tok()})   # a form without the box leaves it alone
    assert schedmod.load(jf)["scheduled"] is False


def test_poster_runs_do_not_warn_about_overrides_for_titles_the_other_server_has(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    score: 9\n  - match: {imdb: tt9}\n    score: 8\n  - match: {imdb: tt404}\n    score: 7\n    server: jellyfin\n")
    srv = MetaJelly()                                                                  # has tt9 only
    two_cfg, one_cfg = dict(TWO), {"servers": {"jellyfin": {"url": "http://j:8096"}}}
    scfg = NS(kind="jellyfin", library=None)
    items, overrides, events, per = actions.poster_context(actions.Workspace(two_cfg, scfg, srv, None, None, tmp_path), f, True)
    assert overrides == {"abc123": 8.0}
    assert [e.kind for e in events] == ["warn", "info"]                                # tt404 is limited to this server: a real problem; tt1 is just on the other one
    assert "no item matches" in events[0].message and "1 override(s) are for titles that are not on this server" in events[1].message
    items, overrides, events, per = actions.poster_context(actions.Workspace(one_cfg, scfg, srv, None, None, tmp_path), f, True)
    assert [e.kind for e in events] == ["warn", "warn"] and not any(e.kind == "info" for e in events)   # a single server: as before
