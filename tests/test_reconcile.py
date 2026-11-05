import hashlib
from types import SimpleNamespace

import pytest

from laminator.actions import Unsure, get_original
from laminator.store import Store


def sha(b):
    return hashlib.sha256(b).hexdigest()


class FakeSrv:
    def __init__(self, data):
        self.data = data

    def download_primary(self, _id):
        return self.data, "image/jpeg"


def item(tag):
    return SimpleNamespace(id="1", name="X", poster_tag=tag)


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "s.db")
    srv = FakeSrv(b"ORIGINAL")
    od = tmp_path / "o"; od.mkdir()
    row, data, _ = get_original(srv, store, od, item("t1"))   # first sight
    assert data == b"ORIGINAL"
    overlay = b"OVERLAY"
    store.set_applied("1", "h", "7.3", "t2", sha(overlay))  # we uploaded an overlay
    return store, srv, od


def test_tag_only_change_is_ignored(env):
    store, srv, od = env
    srv.data = b"OVERLAY"
    row, data, _ = get_original(srv, store, od, item("t3"))
    assert data == b"ORIGINAL" and row["applied_hash"] == "h" and row["served_tag"] == "t3"


def test_overlay_lost_marks_for_reapply(env):
    store, srv, od = env
    srv.data = b"ORIGINAL"
    row, data, ev = get_original(srv, store, od, item("t3"))
    assert data == b"ORIGINAL" and row["applied_hash"] is None
    assert [e.kind for e in ev] == ["lost"]


def test_user_poster_is_adopted_and_old_kept(env):
    store, srv, od = env
    srv.data = b"USER CHOSE THIS"
    row, data, ev = get_original(srv, store, od, item("t3"))
    assert data == b"USER CHOSE THIS" and row["applied_hash"] is None
    assert [e.kind for e in ev] == ["new"]
    assert (od / "1.img").read_bytes() == b"ORIGINAL"          # old original kept


def test_unknown_state_is_not_guessed(env):
    store, srv, od = env
    store.db.execute("UPDATE posters SET served_sha=NULL"); store.db.commit()
    srv.data = b"SOMETHING"
    with pytest.raises(Unsure):
        get_original(srv, store, od, item("t3"))


# ---- our own overlays are recognised and never taken for an original ---------------------------------------------------------
def overlaid():
    import io
    from PIL import Image
    from laminator.assets import load_template
    from laminator.render import render
    from pathlib import Path
    buf = io.BytesIO(); Image.new("RGB", (200, 300), (30, 60, 90)).save(buf, "JPEG")
    tpl, _ = load_template("default", Path("."))
    return buf.getvalue(), render(buf.getvalue(), "7.3", tpl)


def test_rendered_posters_carry_the_marker_and_plain_ones_do_not():
    from laminator.render import is_overlaid
    plain, drawn = overlaid()
    assert is_overlaid(drawn) and not is_overlaid(plain) and not is_overlaid(b"ORIGINAL") and not is_overlaid(b"")


def test_a_fresh_data_folder_refuses_a_poster_that_is_already_our_overlay(tmp_path):
    plain, drawn = overlaid()
    store = Store(tmp_path / "s.db"); od = tmp_path / "o"; od.mkdir()
    with pytest.raises(Unsure, match="already carries a laminator overlay"):
        get_original(FakeSrv(drawn), store, od, item("t1"))
    assert store.get("1") is None and not list(od.iterdir())                    # nothing was saved as an "original"
    row, data, _ = get_original(FakeSrv(plain), store, od, item("t1"))          # a plain poster is fine
    assert data == plain


def test_a_changed_poster_that_is_our_overlay_is_not_adopted_either(tmp_path):
    plain, drawn = overlaid()
    store = Store(tmp_path / "s.db"); od = tmp_path / "o"; od.mkdir()
    srv = FakeSrv(plain)
    get_original(srv, store, od, item("t1"))
    store.set_applied("1", "h", "7.3", "t2", sha(b"SOMETHING WE UPLOADED"))
    srv.data = drawn                                                              # e.g. another instance's overlay, or a re-encoded one
    with pytest.raises(Unsure, match="already carries a laminator overlay"):
        get_original(srv, store, od, item("t3"))
    assert store.get("1")["original_sha"] == sha(plain) and sorted(p.name for p in od.iterdir()) == ["1.img"]
