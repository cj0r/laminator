import json
import os
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest

from laminator import cleanup, store
from laminator.cleanup import DAY, Limits
from laminator.config import ConfigError

T0 = 1_800_000_000.0   # a fixed "now" so ages are exact


class Srv:
    def __init__(self, ids): self.ids, self.fail = set(ids), None
    def libraries(self):
        if self.fail: raise self.fail
        return [{"Name": "Movies", "CollectionType": "movies"}]
    def items(self, lib=None, limit=None): return [NS(id=i, name=f"Item{i}") for i in sorted(self.ids)]


def age(path: Path, days: float, now=T0):
    os.utime(path, (now - days * DAY, now - days * DAY), follow_symlinks=False)


def make(tmp_path, managed=("1", "2", "3", "4", "5", "6"), present=None):
    root = tmp_path / "data"; sd = root / "servers" / "emby"
    (sd / "originals").mkdir(parents=True)
    st = store.Store(sd / "state.db")
    for i in managed:
        (sd / "originals" / f"{i}.img").write_bytes(b"ORIG" + i.encode())
        (sd / "originals" / f"{i}-abcd1234.img").write_bytes(b"HIST" + i.encode())
        st.put_original(i, f"Item{i}", str(sd / "originals" / f"{i}.img"), "sha" + i, "t")
    ws = NS(srv=Srv(managed if present is None else present), store=st, data=sd, root=root, scfg=NS(kind="emby"), originals=sd / "originals")
    return ws


def run(ws, now, lim=None, dry=False):
    return list(cleanup.run(ws, now, lim or Limits(), dry_run=dry))


def files(ws, item): return sorted(p.name for p in (ws.data / "originals").glob(f"{item}*.img"))


# ---- vanished items: grace period -------------------------------------------------------------------------
def test_a_vanished_item_is_only_marked_at_first_and_kept_through_the_grace_period(tmp_path):
    ws = make(tmp_path, present=("1", "2", "3", "4", "5"))                       # item 6 is gone from the server
    ev = run(ws, T0)
    assert "6" in ws.store.missing_map() and ws.store.missing_map()["6"] == T0
    assert files(ws, "6") == ["6-abcd1234.img", "6.img"] and "6" in ws.store.managed_ids()   # nothing removed
    assert any("still inside the grace period" in e.message for e in ev)
    run(ws, T0 + 29 * DAY)
    assert files(ws, "6") == ["6-abcd1234.img", "6.img"] and ws.store.missing_map()["6"] == T0   # re-checking never resets the clock


def test_after_the_grace_period_it_goes_to_trash_with_its_records_and_nothing_else_is_touched(tmp_path):
    ws = make(tmp_path, present=("1", "2", "3", "4", "5"))
    run(ws, T0)
    ws.store.meta_put("6", "Item6", "rating", "PG", "R", True)
    ev = run(ws, T0 + 31 * DAY)
    trash = next((ws.data / "trash").iterdir())
    assert sorted(p.name for p in (trash / "originals").iterdir()) == ["6-abcd1234.img", "6.img"]
    assert (trash / "originals" / "6.img").read_bytes() == b"ORIG6"                                  # moved intact, not deleted
    rows = json.loads((trash / "rows.json").read_text())["6"]
    assert rows["name"] == "Item6" and rows["posters"][0]["original_sha"] == "sha6" and rows["meta_backups"][0]["key"] == "rating"
    assert "6" not in ws.store.managed_ids() and "6" not in ws.store.missing_map() and files(ws, "6") == []
    for i in "12345":
        assert files(ws, i) == [f"{i}-abcd1234.img", f"{i}.img"] and i in ws.store.managed_ids()
    assert any("gone from the server" in e.message and "trash/" in e.message for e in ev)
    assert run(ws, T0 + 32 * DAY)[-1].message == "nothing to clean up"                               # idempotent


def test_an_item_that_comes_back_is_unmarked_even_after_weeks(tmp_path):
    ws = make(tmp_path, present=("1", "2", "3", "4", "5"))
    run(ws, T0)
    ws.srv.ids.add("6")                                                                                # the NAS is back
    run(ws, T0 + 40 * DAY)
    assert ws.store.missing_map() == {} and files(ws, "6") == ["6-abcd1234.img", "6.img"] and not (ws.data / "trash").exists()


def test_it_is_only_purged_if_still_missing_at_the_moment_of_purging(tmp_path):
    ws = make(tmp_path, present=("1", "2", "3", "4", "5"))
    run(ws, T0)                                                                                        # marked
    ws.srv.ids.add("6")                                                                                # back just before the purge run
    run(ws, T0 + 90 * DAY)
    assert "6" in ws.store.managed_ids() and files(ws, "6") and ws.store.missing_map() == {}


def test_a_metadata_only_item_that_is_gone_is_cleaned_too(tmp_path):
    ws = make(tmp_path, managed=("1", "2", "3", "4"), present=("1", "2", "3", "4"))
    ws.store.meta_put("77", "Gone Film", "critic_rating", None, 73, True)                              # never had a poster record
    run(ws, T0)
    run(ws, T0 + 31 * DAY)
    assert "77" not in ws.store.managed_ids()
    assert json.loads((next((ws.data / "trash").iterdir()) / "rows.json").read_text())["77"]["meta_backups"][0]["key"] == "critic_rating"


# ---- outages must never look like deletions -------------------------------------------------------------------------
def test_losing_most_of_the_library_at_once_is_an_outage_not_deletions(tmp_path):
    ws = make(tmp_path, present=("1", "2"))                                                            # 4 of 6 vanish together
    ev = run(ws, T0)
    assert ws.store.missing_map() == {} and any(e.kind == "warn" and "offline" in e.message for e in ev)
    run(ws, T0 + 400 * DAY)
    assert all(files(ws, i) for i in "123456") and len(ws.store.managed_ids()) == 6


def test_an_empty_listing_is_an_outage(tmp_path):
    ws = make(tmp_path, present=())
    ev = run(ws, T0)
    assert any("outage" in e.message for e in ev) and ws.store.missing_map() == {}


def test_the_threshold_is_more_than_half_and_at_least_three(tmp_path):
    ws = make(tmp_path, present=("1", "2", "3"))                                                       # exactly half gone: not "more than half"
    run(ws, T0)
    assert set(ws.store.missing_map()) == {"4", "5", "6"}
    ws2 = make(tmp_path / "b", managed=("1", "2", "3"), present=("1",))                                # only 2 gone: below the minimum of 3
    run(ws2, T0)
    assert set(ws2.store.missing_map()) == {"2", "3"}


@pytest.mark.parametrize("err", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), RuntimeError("anything")])
def test_a_server_error_judges_nothing_but_local_housekeeping_still_runs(tmp_path, err):
    ws = make(tmp_path)
    ws.srv.fail = err
    (ws.data / "out").mkdir(); old = ws.data / "out" / "old.jpg"; old.write_bytes(b"x"); age(old, 60)
    ev = run(ws, T0)
    assert any(e.kind == "warn" and "could not list the server" in e.message for e in ev)
    assert ws.store.missing_map() == {} and all(files(ws, i) for i in "123456") and not old.exists()


def test_dry_run_changes_nothing_at_all(tmp_path):
    ws = make(tmp_path, present=("1", "2", "3", "4", "5"))
    run(ws, T0)
    (ws.data / "out").mkdir(); o = ws.data / "out" / "old.jpg"; o.write_bytes(b"x"); age(o, 60)
    ev = run(ws, T0 + 31 * DAY, dry=True)
    assert any("gone from the server, past the grace period" in e.message and "Item6" in e.message for e in ev)
    assert o.exists() and files(ws, "6") and "6" in ws.store.managed_ids() and not (ws.data / "trash").exists()
    ws2 = make(tmp_path / "c", present=("1", "2", "3", "4", "5"))
    run(ws2, T0, dry=True)
    assert ws2.store.missing_map() == {}                                                               # a plan does not even mark


# ---- orphans, old renders, caches, exports, trash ----------------------------------------------------------------------------
def test_orphan_originals_go_to_trash_and_unexpected_files_are_left_alone(tmp_path):
    ws = make(tmp_path)
    od = ws.data / "originals"
    (od / "99.img").write_bytes(b"orphan"); (od / "99-deadbeef.img").write_bytes(b"orphan2")
    (od / "notes.txt").write_text("mine"); (od / "abc-zzzzzzzz.img").write_bytes(b"odd"); (od / "readme.IMG").write_bytes(b"odd2")
    outside = tmp_path / "outside.img"; outside.write_bytes(b"precious"); (od / "98.img").symlink_to(outside)
    run(ws, T0)
    trash = next((ws.data / "trash").iterdir())
    assert sorted(p.name for p in (trash / "originals").iterdir()) == ["99-deadbeef.img", "99.img"]
    assert (od / "notes.txt").exists() and (od / "abc-zzzzzzzz.img").exists() and (od / "readme.IMG").exists()
    assert (od / "98.img").is_symlink() and outside.read_bytes() == b"precious"                      # a link is never followed or moved
    assert all(files(ws, i) for i in "123456")


def test_old_renders_caches_and_exports_are_deleted_young_ones_kept(tmp_path):
    ws = make(tmp_path)
    for d in ("out", ):
        (ws.data / d).mkdir()
    (ws.root / "cache" / "posters").mkdir(parents=True); (ws.root / "cache" / "mdblist").mkdir(parents=True); (ws.root / "exports").mkdir()
    (ws.root / "cache" / "asset-thumbs").mkdir(parents=True)
    spec = [(ws.root / "cache" / "asset-thumbs" / "old.jpg", 15, False), (ws.root / "cache" / "asset-thumbs" / "new.jpg", 2, True),
            (ws.data / "out" / "old.jpg", 31, False), (ws.data / "out" / "new.jpg", 5, True),
            (ws.root / "cache" / "posters" / "old.img", 15, False), (ws.root / "cache" / "posters" / "new.img", 2, True),
            (ws.root / "cache" / "mdblist" / "old.json", 8, False), (ws.root / "cache" / "mdblist" / "new.json", 3, True),
            (ws.root / "exports" / "laminator-posters-emby-20200101-000000.zip", 15, False), (ws.root / "exports" / "laminator-posters-emby-20260101-000000.zip", 1, True),
            (ws.root / "exports" / "my-own-notes.zip", 400, True)]                                      # not one of ours: never touched
    for p, days, keep in spec:
        p.write_bytes(b"x"); age(p, days)
    run(ws, T0)
    for p, days, keep in spec:
        assert p.exists() == keep, p.name


def test_trash_is_kept_for_the_retention_then_deleted_for_good(tmp_path):
    ws = make(tmp_path)
    old, young = ws.data / "trash" / "20260101-000000", ws.data / "trash" / "20261001-000000"
    for d in (old, young):
        (d / "originals").mkdir(parents=True); (d / "originals" / "x.img").write_bytes(b"x")
    age(old, 91); age(young, 10)
    rt = ws.root / "trash" / "20260101-000000"; (rt).mkdir(parents=True); age(rt, 120)
    run(ws, T0)
    assert not old.exists() and young.exists() and not rt.exists()


def test_deletion_never_leaves_laminators_own_folders(tmp_path):
    ws = make(tmp_path)
    victim = tmp_path / "victim"; victim.mkdir(); (victim / "keep.txt").write_text("mine")
    (ws.data / "out").mkdir(); link = ws.data / "out" / "link"; link.symlink_to(victim, target_is_directory=True)
    (ws.data / "trash").mkdir(); tl = ws.data / "trash" / "20200101-000000"; tl.symlink_to(victim, target_is_directory=True)
    age(link, 90); age(tl, 400)
    run(ws, T0)
    assert (victim / "keep.txt").read_text() == "mine" and link.is_symlink() and tl.is_symlink()


def test_old_safety_copies_are_opt_in_and_go_to_trash(tmp_path):
    ws = make(tmp_path)
    arc = ws.data / "archive-20250101-000000"; (arc / "originals").mkdir(parents=True); (arc / "originals" / "a.img").write_bytes(b"a"); age(arc, 200)
    pre = ws.root / "pre-restore-20250101-000000"; pre.mkdir(); (pre / "settings.yaml").write_text("x: 1"); age(pre, 200)
    run(ws, T0)
    assert arc.exists() and pre.exists()                                                               # off by default
    run(ws, T0, Limits(safety_days=180))
    assert not arc.exists() and not pre.exists()
    assert (next((ws.data / "trash").iterdir()) / "safety-copies" / "archive-20250101-000000" / "originals" / "a.img").read_bytes() == b"a"
    assert (next((ws.root / "trash").iterdir()) / "pre-restore-20250101-000000" / "settings.yaml").exists()      # still recoverable


def test_limits_come_from_config_and_are_validated():
    assert cleanup.Limits.from_cfg({}).grace_days == 30 and cleanup.Limits.from_cfg({}).trash_days == 90
    assert cleanup.Limits.from_cfg({"cleanup": {"grace_days": 14, "trash_days": 200}}).grace_days == 14
    for bad in ({"grace_days": 3}, {"grace_days": 400}, {"trash_days": "90"}, {"grace_days": True}, {"trash_days": 1000}):
        with pytest.raises(ConfigError):
            cleanup.Limits.from_cfg({"cleanup": bad})
    with pytest.raises(ConfigError):
        cleanup.Limits.from_cfg({"cleanup": [1]})
