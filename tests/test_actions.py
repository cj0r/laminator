from pathlib import Path

import pytest

from laminator import actions, assets
from laminator.actions import Busy, Event
from laminator.config import ConfigError


def test_event_lines_match_the_cli_output():
    assert Event("render", "Elf (2003)", "aggregate 7.3").line() == "render Elf (2003): aggregate 7.3"
    assert Event("apply", "Elf", "aggregate 7.3").line() == "apply  Elf: aggregate 7.3"
    assert Event("same", "Elf", "x").line() == "same   Elf: x"
    assert Event("restore", "Elf").line() == "restore Elf"
    assert Event("warn", message="score override ignored: y").line() == "WARN   score override ignored: y"
    assert Event("error", "Elf", "boom").line() == "ERROR  Elf: boom"
    assert Event("info", message="critic rating: on").line() == "critic rating: on"
    assert Event("warn").problem and Event("error").problem and not Event("apply").problem


def test_scope_guard():
    with pytest.raises(ConfigError):
        actions.require_scope(None, None, False)
    actions.require_scope("Elf", None, False)
    actions.require_scope(None, "Movies", False)
    actions.require_scope(None, None, True)
    actions.require_scope(None, None, False, "Movies")


def test_run_lock_blocks_a_second_run(tmp_path):
    with actions.run_lock(tmp_path):
        with pytest.raises(Busy):
            with actions.run_lock(tmp_path):
                pass
    with actions.run_lock(tmp_path):  # released afterwards
        pass


def test_fonts_prefer_the_data_folder_then_bundled(tmp_path):
    bundled = assets.font_path("fonts/Anton-Regular.ttf", tmp_path)
    assert bundled.exists() and "assets" in str(bundled)
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "Anton-Regular.ttf").write_bytes(b"mine")
    assert assets.font_path("Anton-Regular.ttf", tmp_path) == tmp_path / "fonts" / "Anton-Regular.ttf"
    with pytest.raises(FileNotFoundError):
        assets.font_path("Nope.ttf", tmp_path)
    assert "Anton-Regular.ttf" in assets.list_fonts(tmp_path)


def test_templates_by_name_and_signature_ignores_font_location(tmp_path):
    tpl, sig = assets.load_template("default", tmp_path)
    assert Path(tpl["score"]["font"]).exists()
    assert "assets" not in sig            # the signature is of the template as written, not the resolved path
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates" / "default.yaml").write_text(
        "sources: [imdb]\ngradient: {height: 0.1, color: [0,0,0], max_opacity: 0.5}\n"
        "score: {font: Anton-Regular.ttf, size: 0.05, color: [255,255,255], margin_x: 0.05, margin_y: 0.02}\n")
    mine, _ = assets.load_template("default", tmp_path)  # data folder wins over the bundled one
    assert mine["sources"] == ["imdb"]
    with pytest.raises(FileNotFoundError):
        assets.load_template("missing", tmp_path)


def test_key_variables_are_named_after_the_service(monkeypatch):
    from laminator import config
    assert config.server_key_env({"server": {"kind": "emby"}}) == "API_EMBY"
    assert config.server_key_env({"server": {"kind": "jellyfin"}}) == "API_JELLYFIN"
    assert config.server_key_env({"server": {"kind": "emby", "key_env": "MY_KEY"}}) == "MY_KEY"
    assert config.MDBLIST_ENV == "API_MDBLIST"
    for v in ("API_EMBY", "LAMINATOR_SERVER_KEY"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(ConfigError, match="set API_EMBY"):
        config.require_env("API_EMBY", "x")
    monkeypatch.setenv("LAMINATOR_SERVER_KEY", "old")      # an old .env on another machine: say what to rename
    with pytest.raises(ConfigError, match="LAMINATOR_SERVER_KEY was renamed to API_EMBY"):
        config.require_env("API_EMBY", "x")
    monkeypatch.setenv("API_EMBY", "new")
    assert config.require_env("API_EMBY", "x") == "new"
