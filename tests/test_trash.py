import json
import os
import time
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from laminator import cleanup, store, trash
from laminator.cleanup import DAY, Limits
from test_cleanup import Srv, T0, age, make, run

QUOTE = "Bob's \"Film\"; DROP TABLE posters; --"


def trashed(tmp_path, gone=("6",), name_of=None):
    """A server where `gone` items left, a month-plus ago, then cleanup moved them to trash."""
    present = tuple(i for i in "123456" if i not in gone)
    ws = make(tmp_path, present=present)
    for g in gone:
        ws.store.db.execute("UPDATE posters SET name=? WHERE item_id=?", (name_of or f"Item{g}", g)); ws.store.db.commit()
    ws.store.meta_put(gone[0], "Item", "rating", "PG", "R", True)
    run(ws, T0); run(ws, T0 + 31 * DAY)
    return ws


def restore(ws, stamp, item=None, now=T0 + 40 * DAY):
    return list(trash.restore(ws, stamp, item, now))


def batch(ws): return trash.list_batches(ws, T0 + 40 * DAY)[0]


def test_the_trash_is_listed_with_what_is_in_it_and_how_long_it_is_kept(tmp_path):
    ws = trashed(tmp_path)
    b = batch(ws)
    assert b.scope == "server" and [(i.item_id, i.name, len(i.files), i.has_rows) for i in b.items] == [("6", "Item6", 2, True)]
    assert b.items[0].missing_since == T0 and b.bytes > 0
    assert 89 < b.keep_days_left + b.age_days < 91 and b.keep_days_left < 90                         # 90 days from when it went in


def test_restoring_an_item_puts_back_its_posters_and_records_and_the_empty_batch_goes(tmp_path):
    ws = trashed(tmp_path)
    b = batch(ws)
    ev = restore(ws, b.stamp, "6")
    assert any(e.kind == "restore" and "2 saved poster(s) and 2 record(s)" in e.message for e in ev)     # 1 poster row + 1 metadata row
    assert sorted(p.name for p in ws.originals.glob("6*.img")) == ["6-abcd1234.img", "6.img"]
    assert (ws.originals / "6.img").read_bytes() == b"ORIG6"
    row = ws.store.get("6")
    assert row["name"] == "Item6" and row["original_sha"] == "sha6" and Path(row["original_path"]) == ws.originals / "6.img"
    assert ws.store.meta_get("6")["rating"]["override_value"] == json.dumps("R") and "6" in ws.store.managed_ids()
    assert "6" not in ws.store.missing_map()                                                         # a fresh start: not marked
    assert not (ws.data / "trash").exists() or not list((ws.data / "trash").iterdir())               # everything is back: the batch is gone


def test_a_restored_item_that_is_still_gone_starts_a_new_grace_period_instead_of_vanishing_again(tmp_path):
    ws = trashed(tmp_path)
    restore(ws, batch(ws).stamp, "6")
    ws.srv.ids.discard("6")
    run(ws, T0 + 41 * DAY)
    assert ws.store.missing_map()["6"] == T0 + 41 * DAY and "6" in ws.store.managed_ids()
    run(ws, T0 + 60 * DAY)
    assert "6" in ws.store.managed_ids()                                                              # 19 days later: still waiting
    run(ws, T0 + 72 * DAY)
    assert "6" not in ws.store.managed_ids()                                                          # and only then purged again


def test_it_never_restores_over_records_that_exist_again(tmp_path):
    ws = trashed(tmp_path)
    b = batch(ws)
    ws.store.put_original("6", "A New Item 6", str(ws.originals / "new.img"), "newsha", "t")          # the id is in use again
    ev = restore(ws, b.stamp, "6")
    assert any(e.kind == "warn" and "already has records again" in e.message for e in ev)
    assert ws.store.get("6")["name"] == "A New Item 6" and not (ws.originals / "6.img").exists() and b.path.exists()


def test_existing_files_are_never_overwritten(tmp_path):
    ws = trashed(tmp_path)
    b = batch(ws)
    (ws.originals / "6.img").write_bytes(b"SOMETHING ELSE")                                           # different content already there
    (ws.originals / "6-abcd1234.img").write_bytes(b"HIST6")                                           # identical content already there
    ev = restore(ws, b.stamp, "6")
    assert (ws.originals / "6.img").read_bytes() == b"SOMETHING ELSE" and any("kept the trash copy" in e.message for e in ev)
    assert (b.path / "originals" / "6.img").exists()                                                   # the trash copy is still there
    assert not (b.path / "originals" / "6-abcd1234.img").exists()                                      # the identical one was just dropped


def test_a_tampered_rows_file_cannot_inject_or_escape(tmp_path):
    ws = trashed(tmp_path, name_of=QUOTE)
    b = batch(ws)
    rows = json.loads((b.path / "rows.json").read_text())
    good = rows["6"]["posters"][0]
    good["original_path"] = "/some/old/machine/servers/emby/originals/6.img"                          # a stale path: re-pointed, not trusted
    hostile = dict(good, original_path="../../../../etc/passwd")                                         # refused: not one of our file names
    rows["6"]["posters"] += [hostile, dict(good, item_id="7", original_path="7.img"), dict(good, original_sha=123), dict(good, extra="column"),
                             "garbage", {"item_id": "6"}]
    rows["6"]["meta_backups"] += [dict(rows["6"]["meta_backups"][0], key="__import__('os')"), {"item_id": "6", "key": "rating"}, 5]
    rows["notadict"], rows["6x"] = "oops", {"posters": "no", "meta_backups": [1, 2]}
    (b.path / "rows.json").write_text(json.dumps(rows))
    ev = restore(ws, b.stamp, "6")
    assert [r["item_id"] for r in ws.store.all()].count("6") == 1                                        # exactly the one valid record
    assert Path(ws.store.get("6")["original_path"]) == ws.originals / "6.img"                            # re-pointed at this server's folder
    assert ws.store.get("7") is None and any("not restored" in e.message for e in ev)                    # no record for another item; the hostile path refused
    assert set(ws.store.meta_get("6")) == {"rating"}                                                     # the bogus key and junk rows were ignored
    assert ws.store.get("6")["name"] == QUOTE and ws.store.db.execute("SELECT count(*) FROM posters").fetchone()[0] == 6   # the table survived the quotes
    assert not (Path("/etc/passwd.img")).exists()


def test_a_record_whose_poster_is_missing_is_not_restored_and_stays_in_the_trash(tmp_path):
    ws = trashed(tmp_path)
    b = batch(ws)
    (b.path / "originals" / "6.img").unlink(); (b.path / "originals" / "6-abcd1234.img").unlink()      # the pictures are gone from the trash
    ev = restore(ws, b.stamp, "6")
    assert any("saved poster is not there" in e.message for e in ev) and ws.store.get("6") is None
    kept = json.loads((b.path / "rows.json").read_text())["6"]
    assert b.path.exists() and len(kept["posters"]) == 1 and kept["meta_backups"] == []                  # the poster record stays; the metadata one was restored
    assert set(ws.store.meta_get("6")) == {"rating"}
    (b.path / "rows.json").write_text("{not json")
    assert isinstance(trash.list_batches(ws, T0), list)                                                 # a damaged rows.json does not crash the listing


def test_partial_restore_keeps_the_rest_in_the_batch(tmp_path):
    ws = trashed(tmp_path, gone=("5", "6"))
    b = batch(ws)
    assert sorted(i.item_id for i in b.items) == ["5", "6"]
    restore(ws, b.stamp, "5")
    left = batch(ws)
    assert [i.item_id for i in left.items] == ["6"] and "5" in ws.store.managed_ids() and (left.path / "originals" / "6.img").exists()
    assert "5" not in json.loads((left.path / "rows.json").read_text())


def test_orphans_and_safety_copies_come_back_with_restore_everything(tmp_path):
    ws = make(tmp_path)
    (ws.originals / "99.img").write_bytes(b"orphan")
    arc = ws.data / "archive-20250101-000000"; arc.mkdir(); (arc / "x.txt").write_text("old"); age(arc, 300)
    run(ws, T0, Limits(safety_days=180))
    b = batch(ws)
    assert b.orphans and b.safety and not (ws.originals / "99.img").exists() and not arc.exists()
    ev = restore(ws, b.stamp)
    assert (ws.originals / "99.img").read_bytes() == b"orphan" and (arc / "x.txt").read_text() == "old"
    assert any("safety copy put back" in e.message for e in ev)


def test_bad_names_symlinks_and_unknown_batches_are_refused(tmp_path):
    ws = trashed(tmp_path)
    for bad in ("../x", "20261101", "", "20261101-1200000", "..", "20261101-120000/../x"):
        with pytest.raises((ValueError, FileNotFoundError)):
            list(trash.restore(ws, bad))
    with pytest.raises(FileNotFoundError):
        list(trash.restore(ws, "20200101-000000"))
    elsewhere = tmp_path / "elsewhere"; (elsewhere / "originals").mkdir(parents=True); (elsewhere / "originals" / "5.img").write_bytes(b"x")
    (ws.data / "trash" / "20250101-000000").symlink_to(elsewhere, target_is_directory=True)
    assert all(b.stamp != "20250101-000000" for b in trash.list_batches(ws, T0))                        # a link is not a batch
