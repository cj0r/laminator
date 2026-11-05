import io
import os
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from laminator import baseposters as bp
from laminator.actions import Unsure, get_original, poster_history, sha
from laminator.store import Store
from test_reconcile import FakeSrv


def img(color=(10, 20, 30), fmt="JPEG", size=(200, 300)):
    b = io.BytesIO(); Image.new("RGB", size, color).save(b, fmt); return b.getvalue()


def movie(path="/media/movies/Dumbo/dumbo.mkv", lib="Movies", name="Dumbo", year=1941, typ="Movie", iid="1"):
    return NS(id=iid, name=name, year=year, type=typ, path=path, library=lib, poster_tag="t1")


def test_keys_follow_the_media_folder_then_the_library_then_the_file_name():
    k = bp.keys_for(movie())
    assert k[:4] == ["movies/dumbo/poster", "movies/dumbo", "dumbo/poster", "dumbo"] and "dumbo" in k
    assert bp.keys_for(movie(path=r"D:\Films\Fan Edit (2020)\fan.edit.mkv"))[2] == "fan edit (2020)/poster"        # a Windows path works too
    show = bp.keys_for(movie(path="/media/tv/Ozark", typ="Series", lib="TV"))
    assert "tv/ozark/poster" in show and not any("ozark/ozark" in x for x in show)                                   # a show uses its own folder
    assert bp.keys_for(movie(path=None))[2] == "dumbo (1941)/poster"                                                  # no path: Name (Year)


def test_hostile_names_from_the_server_never_leave_the_assets_folder():
    for bad in ("/media/movies/../../etc/passwd/x.mkv", "/media/movies/./x.mkv", "/media/..", ""):
        keys = bp.keys_for(movie(path=bad, name="..", year=None))
        assert all(".." not in k.split("/") and not k.startswith("/") for k in keys), (bad, keys)
    assert bp.folder_name(movie(path="/m/a\x00b/f.mkv")) is None


def test_the_index_finds_posters_case_insensitively_and_ignores_junk_and_escaping_links(tmp_path):
    root = tmp_path / "assets"; (root / "Dumbo").mkdir(parents=True); (root / "Movies" / "Elf").mkdir(parents=True); (root / ".hidden").mkdir()
    (root / "Dumbo" / "POSTER.JPG").write_bytes(img()); (root / "Movies" / "Elf" / "poster.png").write_bytes(img(fmt="PNG"))
    (root / "notes.txt").write_text("x"); (root / ".hidden" / "poster.jpg").write_bytes(img())
    outside = tmp_path / "secret.jpg"; outside.write_bytes(img()); (root / "Evil.jpg").symlink_to(outside)
    idx = bp.AssetIndex(root)
    assert idx.find(movie()).name == "POSTER.JPG" and idx.find(movie(path="/m/Elf/elf.mkv", name="Elf")).name == "poster.png"
    assert idx.find(movie(path="/m/Evil/e.mkv", name="Evil")) is None and ".hidden/poster" not in idx.files
    assert bp.AssetIndex(tmp_path / "nope").files == {}


def test_bad_images_are_refused_with_a_sentence():
    for data, msg in ((b"", "empty"), (b"not an image", "not a readable image"), (img(fmt="GIF"), "JPEG, PNG or WebP"),
                      (b"x" * (bp.MAX_BYTES + 1), "larger")):
        with pytest.raises(bp.AssetError, match=msg):
            bp.check_bytes(data)
    assert bp.check_bytes(img()) == ".jpg" and bp.check_bytes(img(fmt="PNG")) == ".png" and bp.check_bytes(img(fmt="WEBP")) == ".webp"


def test_a_poster_that_is_already_our_overlay_is_refused_so_nothing_stacks():
    from laminator.assets import load_template
    from laminator.render import render
    tpl, _ = load_template("default", Path("."))
    with pytest.raises(bp.AssetError, match="already carries"):
        bp.check_bytes(render(img(), "7.3", tpl))


def test_first_sight_uses_the_custom_poster_and_keeps_the_servers_own_in_the_history(tmp_path):
    store = Store(tmp_path / "s.db"); od = tmp_path / "o"; od.mkdir()
    mine = tmp_path / "poster.jpg"; mine.write_bytes(img((200, 0, 0)))
    row, data, ev = get_original(FakeSrv(img((0, 0, 200))), store, od, movie(), mine)
    assert data == mine.read_bytes() and [e.kind for e in ev] == ["new"] and "custom poster" in ev[0].message
    assert row["original_sha"] == sha(mine.read_bytes())
    hist = poster_history(od, "1", row["original_sha"])
    assert len(hist) == 2 and hist[-1]["first"] and hist[-1]["sha"] == sha(img((0, 0, 200)))                         # the server's poster is a way back


def test_an_unchanged_custom_poster_is_quiet_and_a_new_file_replaces_the_base(tmp_path):
    store = Store(tmp_path / "s.db"); od = tmp_path / "o"; od.mkdir()
    mine = tmp_path / "poster.jpg"; mine.write_bytes(img((200, 0, 0)))
    srv = FakeSrv(img())
    get_original(srv, store, od, movie(), mine)
    _, _, ev = get_original(srv, store, od, movie(), mine)
    assert ev == []
    mine.write_bytes(img((0, 200, 0)))
    row, data, ev = get_original(srv, store, od, movie(), mine)
    assert data == mine.read_bytes() and [e.kind for e in ev] == ["new"] and row["applied_hash"] is None             # redrawn on the next apply
    mine.write_bytes(img((200, 0, 0)))                                                                                  # back to an earlier picture: reused
    _, _, ev = get_original(srv, store, od, movie(), mine)
    assert len(list(od.glob("1*.img"))) == 3


def test_a_fresh_data_folder_can_start_from_a_custom_poster_even_if_the_server_shows_our_overlay(tmp_path):
    from laminator.assets import load_template
    from laminator.render import render
    tpl, _ = load_template("default", Path("."))
    store = Store(tmp_path / "s.db"); od = tmp_path / "o"; od.mkdir()
    mine = tmp_path / "poster.jpg"; mine.write_bytes(img((200, 0, 0)))
    with pytest.raises(Unsure, match="already carries"):
        get_original(FakeSrv(render(img(), "7.3", tpl)), store, od, movie())                                          # without a custom poster: refused
    row, data, _ = get_original(FakeSrv(render(img(), "7.3", tpl)), store, od, movie(), mine)                       # with one: safe base
    assert data == mine.read_bytes() and [p.name for p in od.iterdir()] == ["1.img"]


def test_an_unreadable_custom_poster_is_reported_not_used(tmp_path):
    store = Store(tmp_path / "s.db"); od = tmp_path / "o"; od.mkdir()
    bad = tmp_path / "poster.jpg"; bad.write_bytes(b"junk")
    with pytest.raises(Unsure, match="custom poster poster.jpg: this is not a readable image"):
        get_original(FakeSrv(img()), store, od, movie(), bad)
    assert store.get("1") is None


def test_save_and_remove(tmp_path):
    root = tmp_path / "assets"; it = movie()
    idx = bp.AssetIndex(root)
    p = bp.save(root, idx, it, img(fmt="PNG"))
    assert p == root / "Dumbo" / "poster.png"
    p2 = bp.save(root, bp.AssetIndex(root), it, img())                                                                # another format replaces, no duplicate
    assert p2 == root / "Dumbo" / "poster.jpg" and not p.exists()
    with pytest.raises(bp.AssetError):
        bp.save(root, bp.AssetIndex(root), it, b"junk")
    gone = bp.remove(root, tmp_path, p2)
    assert not p2.exists() and gone.exists() and "assets-removed-" in str(gone)
    with pytest.raises(bp.AssetError, match="not inside"):
        bp.remove(root, tmp_path, tmp_path / "other.jpg")
