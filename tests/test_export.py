import csv
import hashlib
import io
import zipfile
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from laminator import export, store
from laminator.export import Entry, plan_export


def jpg(c=(10, 20, 30)) -> bytes:
    b = io.BytesIO(); Image.new("RGB", (40, 60), c).save(b, "JPEG"); return b.getvalue()


def png() -> bytes:
    b = io.BytesIO(); Image.new("RGB", (40, 60), (1, 2, 3)).save(b, "PNG"); return b.getvalue()


def item(i, name, path, poster=True):
    return NS(id=str(i), name=name, type="Movie", year=2000, provider_ids={}, has_poster=poster, poster_tag="t", path=path)


LIBS = [{"Name": "Movies", "Locations": ["/mnt/movies"]}, {"Name": "TV shows", "Locations": ["/mnt/tv"]}]


def dest_names(plan, ext="jpg"):
    return sorted(export.arcname(e, ext) for e in plan.entries)


def test_one_film_per_folder_gets_poster_jpg_next_to_the_file():
    p = plan_export(LIBS, {"Movies": [item(1, "A", "/mnt/movies/movie-a/movie.a.1080p.mkv"), item(2, "B", "/mnt/movies/movie-b/b.mp4")]})
    assert dest_names(p) == ["movies/movie-a/poster.jpg", "movies/movie-b/poster.jpg"]      # the example from the request


def test_films_sharing_a_folder_are_named_after_their_file_so_nothing_collides():
    p = plan_export(LIBS, {"Movies": [item(1, "Dumbo", "/mnt/movies/dumbo.720p.mkv"), item(2, "Elf", "/mnt/movies/elf.1080p.mkv"),
                                      item(3, "X", "/mnt/movies/shared/x1.mkv"), item(4, "Y", "/mnt/movies/shared/y2.mkv"),
                                      item(5, "Solo", "/mnt/movies/solo/solo.mkv")]})
    assert dest_names(p) == ["movies/dumbo.720p-poster.jpg", "movies/elf.1080p-poster.jpg", "movies/shared/x1-poster.jpg",
                             "movies/shared/y2-poster.jpg", "movies/solo/poster.jpg"]


def test_shows_and_disc_folders_are_folder_items():
    p = plan_export(LIBS, {"TV shows": [item(1, "Ozark", "/mnt/tv/Ozark"), item(2, "Baskets", "/mnt/tv/Baskets")],
                           "Movies": [item(3, "Disc", "/mnt/movies/Some Film (2001)")]})
    assert dest_names(p) == ["movies/Some Film (2001)/poster.jpg", "tv/Baskets/poster.jpg", "tv/Ozark/poster.jpg"]


def test_layout_and_naming_choices():
    items = {"Movies": [item(1, "A", "/mnt/movies/a/a.mkv"), item(2, "B", "/mnt/movies/b.mkv")]}
    assert dest_names(plan_export(LIBS, items, layout="library")) == ["Movies/a/poster.jpg", "Movies/b-poster.jpg"]
    assert dest_names(plan_export(LIBS, items, layout="none")) == ["a/poster.jpg", "b-poster.jpg"]
    assert dest_names(plan_export(LIBS, items, naming="stem")) == ["movies/a/a-poster.jpg", "movies/b-poster.jpg"]
    assert dest_names(plan_export(LIBS, items, naming="poster")) == ["movies/a/poster.jpg", "movies/poster.jpg"]
    assert [e.library for e in plan_export(LIBS, {"Movies": items["Movies"], "TV shows": [item(9, "T", "/mnt/tv/T")]}, only_library="TV shows").entries] == ["TV shows"]
    with pytest.raises(ValueError):
        plan_export(LIBS, items, layout="weird")


@pytest.mark.parametrize("bad", ["/mnt/movies/../../etc/passwd", "/mnt/movies/a/../../x/y.mkv", "/mnt/movies//a.mkv", "/mnt/other/a.mkv",
                                 "/mnt/movies", "/mnt/moviesX/a.mkv", "relative/a.mkv", "/mnt/movies/a\x00.mkv", "/mnt/movies/./a.mkv", ""])
def test_unsafe_or_foreign_paths_are_skipped_never_exported(bad):
    p = plan_export(LIBS, {"Movies": [item(1, "Evil", bad)]})
    assert p.entries == [] and len(p.skipped) == 1


def test_windows_style_paths_work():
    libs = [{"Name": "Movies", "Locations": ["D:\\Media\\Movies"]}]
    p = plan_export(libs, {"Movies": [item(1, "A", "D:\\Media\\Movies\\Film A\\film.a.mkv")]})
    assert dest_names(p) == ["Movies/Film A/poster.jpg"]
    assert plan_export(libs, {"Movies": [item(1, "A", "D:\\Media\\Movies\\..\\Secret\\x.mkv")]}).entries == []


def test_longest_matching_library_folder_wins_and_missing_paths_are_skipped():
    libs = [{"Name": "Movies", "Locations": ["/mnt/a", "/mnt/a/b"]}]
    e = plan_export(libs, {"Movies": [item(1, "A", "/mnt/a/b/film/f.mkv")]}).entries[0]
    assert e.top == "b" and e.dir_rel == "film"
    assert plan_export(libs, {"Movies": [item(2, "NoPath", None)]}).skipped[0][2].startswith("the server reports no file path")


# ---- building the zip -----------------------------------------------------------------------------------------
class Srv:
    def __init__(self, posters): self.posters = posters
    def download_primary(self, i, max_width=None): return self.posters[i], "image/jpeg"
    def libraries(self): return LIBS


def make_ws(tmp_path, posters):
    st = store.Store(tmp_path / "state.db")
    return NS(srv=Srv(posters), store=st, originals=tmp_path / "originals"), st


def read(zpath):
    z = zipfile.ZipFile(zpath)
    return z, {n: z.read(n) for n in z.namelist()}


def test_the_zip_has_each_poster_next_to_its_media_and_a_manifest(tmp_path):
    ws, _ = make_ws(tmp_path, {"1": jpg((1, 1, 1)), "2": jpg((2, 2, 2)), "3": png()})
    plan = plan_export(LIBS, {"Movies": [item(1, "Elf", "/mnt/movies/elf.mkv"), item(2, "Dumbo", "/mnt/movies/dumbo.mkv"),
                                         item(3, "Png", "/mnt/movies/png/png.mkv"), item(4, "NoPoster", "/mnt/movies/np/np.mkv", poster=False)]})
    dest = tmp_path / "out" / "x.zip"
    events = list(export.build(ws, dest, plan))
    z, files = read(dest)
    assert z.testzip() is None
    assert set(files) == {"movies/elf-poster.jpg", "movies/dumbo-poster.jpg", "movies/png/poster.png", "_laminator-export/manifest.csv", "_laminator-export/README.txt"}
    assert files["movies/elf-poster.jpg"] == jpg((1, 1, 1)) and files["movies/png/poster.png"] == png()      # bytes untouched; extension follows the format
    rows = list(csv.DictReader(io.StringIO(files["_laminator-export/manifest.csv"].decode())))
    status = {r["item"]: r["status"] for r in rows}
    assert status["Elf"] == "ok" and "no poster" in status["NoPoster"] and any(e.kind == "export" and "3 poster(s)" in e.message and "1 skipped" in e.message for e in events)
    readme = files["_laminator-export/README.txt"].decode()
    assert "/mnt/movies" in readme and "Extract this zip into the folder that CONTAINS your library folders" in readme and "3 poster(s)" in readme
    assert not list((tmp_path / "out").glob("*.part"))


def test_no_zip_member_can_escape_the_extraction_folder(tmp_path):
    ws, _ = make_ws(tmp_path, {"1": jpg(), "2": jpg()})
    plan = plan_export(LIBS, {"Movies": [item(1, "Ok", "/mnt/movies/ok/ok.mkv"), item(2, "Evil", "/mnt/movies/../../etc/x.mkv")]})
    dest = tmp_path / "x.zip"
    list(export.build(ws, dest, plan))
    for n in zipfile.ZipFile(dest).namelist():
        parts = n.split("/")
        assert not n.startswith("/") and ".." not in parts and "" not in parts and "\\" not in n, n


def test_original_mode_uses_the_saved_original_and_falls_back_to_the_server(tmp_path):
    ws, st = make_ws(tmp_path, {"1": jpg((9, 9, 9)), "2": jpg((8, 8, 8))})
    (tmp_path / "originals").mkdir()
    (tmp_path / "originals" / "1.img").write_bytes(jpg((200, 0, 0)))
    st.put_original("1", "Elf", str(tmp_path / "originals" / "1.img"), "s", "t")
    plan = plan_export(LIBS, {"Movies": [item(1, "Elf", "/mnt/movies/e/elf.mkv"), item(2, "Untouched", "/mnt/movies/u/u.mkv")]})
    _, cur = read(_built(ws, plan, tmp_path, "current"))
    _, org = read(_built(ws, plan, tmp_path, "original"))
    assert cur["movies/e/poster.jpg"] == jpg((9, 9, 9)) and org["movies/e/poster.jpg"] == jpg((200, 0, 0))      # overlay vs original
    assert org["movies/u/poster.jpg"] == jpg((8, 8, 8))                                                          # never touched: the server's own
    with pytest.raises(ValueError):
        list(export.build(ws, tmp_path / "z.zip", plan, which="bogus"))


def _built(ws, plan, tmp_path, which):
    dest = tmp_path / f"{which}.zip"
    list(export.build(ws, dest, plan, which=which))
    return dest


def test_bad_images_and_name_collisions_are_skipped_with_a_reason_not_fatal(tmp_path):
    ws, _ = make_ws(tmp_path, {"1": b"not an image", "2": jpg(), "3": jpg()})
    plan = plan_export(LIBS, {"Movies": [item(1, "Broken", "/mnt/movies/b/b.mkv"), item(2, "First", "/mnt/movies/same/a.mkv"), item(3, "Second", "/mnt/movies/same/b.mkv")]},
                       naming="poster")                                                                          # forced bare names: two films, one folder
    dest = tmp_path / "x.zip"
    events = list(export.build(ws, dest, plan))
    _, files = read(dest)
    assert [n for n in files if n.endswith(".jpg")] == ["movies/same/poster.jpg"]                                 # the first one wins, nothing overwritten
    rows = {r["item"]: r["status"] for r in csv.DictReader(io.StringIO(files["_laminator-export/manifest.csv"].decode()))}
    assert "not a JPEG" in rows["Broken"] or "UnidentifiedImageError" in rows["Broken"] or "skipped" in rows["Broken"]
    assert "same file name as First" in rows["Second"] and sum(e.kind == "warn" for e in events) == 2


def test_manifest_cells_cannot_become_spreadsheet_formulas(tmp_path):
    ws, _ = make_ws(tmp_path, {"1": jpg()})
    plan = plan_export(LIBS, {"Movies": [item(1, '=HYPERLINK("http://evil","x")', "/mnt/movies/=cmd/a.mkv")]})
    dest = tmp_path / "x.zip"
    list(export.build(ws, dest, plan))
    text = zipfile.ZipFile(dest).read("_laminator-export/manifest.csv").decode()
    cells = [c for row in csv.reader(io.StringIO(text)) for c in row]
    assert not any(c[:1] in "=+-@" for c in cells if c)
    assert "'=HYPERLINK" in text
