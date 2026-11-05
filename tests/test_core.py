import io
from PIL import Image
from laminator.mdblist import pick_score
from pathlib import Path
from laminator.assets import font_path
from laminator.render import render

DATA = {"score": 73, "ratings": [{"source": "imdb", "score": 71}, {"source": "tmdb", "score": None}, {"source": "letterboxd", "score": 70}]}
TPL = {
    "gradient": {"height": 0.2, "color": [0, 0, 0], "max_opacity": 0.8},
    "score": {"font": str(font_path("Poppins-Bold.ttf", Path("/nonexistent"))), "size": 0.05, "color": [255, 255, 255],
              "margin_x": 0.04, "margin_y": 0.01, "shadow": True},
}


def test_pick_score_first_available():
    assert pick_score(DATA, ["aggregate", "imdb"]) == ("aggregate", "7.3", 73)
    assert pick_score(DATA, ["tmdb", "letterboxd"]) == ("letterboxd", "7.0", 70)
    assert pick_score(DATA, ["imdb"], decimals=0) == ("imdb", "71", 71)
    assert pick_score(DATA, ["trakt"]) is None


def test_render_changes_only_bottom_and_keeps_size():
    src = Image.new("RGB", (200, 300), (200, 200, 200))
    buf = io.BytesIO(); src.save(buf, "PNG")
    out = Image.open(io.BytesIO(render(buf.getvalue(), "7.1", TPL)))
    assert out.size == (200, 300)
    assert out.getpixel((100, 10)) == (200, 200, 200)   # top untouched
    assert sum(out.getpixel((10, 299))) < 200            # bottom darkened



# ---- the aggregate is replaced by MDBList's average when missing sources drag it down ------------------------------------------
from laminator import mdblist as _mdb  # noqa: E402


def _claydream(**over):
    d = {"score": 44, "score_average": 73, "ratings": [{"source": "imdb", "score": 72}, {"source": "tmdb", "score": 68}, {"source": "trakt", "score": 69},
                                                       {"source": "tomatoes", "score": 93}, {"source": "metacriticuser", "score": None}]}
    d.update(over)
    return d


def test_a_score_dragged_down_by_missing_sources_uses_the_average():
    assert _mdb.pick_score(_claydream(), ["aggregate"]) == ("aggregate", "7.3", 73)                 # 44 vs 73: the average
    assert _mdb.pick_score(_claydream(score=0), ["aggregate"]) == ("aggregate", "7.3", 73)          # a 0 used to mean no badge at all
    assert _mdb.pick_score(_claydream(score=68), ["aggregate"]) == ("aggregate", "7.3", 73)         # a gap of exactly 5 points counts
    assert _mdb.pick_score(_claydream(score=69), ["aggregate"]) == ("aggregate", "6.9", 69)         # a smaller gap keeps MDBList's own score


def test_a_genuinely_poor_film_keeps_its_low_score_and_other_sources_are_untouched():
    leprechaun = {"score": 41, "score_average": 41, "ratings": [{"source": "imdb", "score": 38}]}
    assert _mdb.pick_score(leprechaun, ["aggregate"]) == ("aggregate", "4.1", 41)
    assert _mdb.pick_score(_claydream(), ["imdb", "aggregate"]) == ("imdb", "7.2", 72)             # a named source is shown as it is
    assert _mdb.pick_score({"score": 44, "ratings": []}, ["aggregate"]) == ("aggregate", "4.4", 44)  # no average given: nothing to compare with
    assert _mdb.pick_score({"score": None, "score_average": 80, "ratings": []}, ["aggregate"]) is None  # nothing is invented without MDBList's score
