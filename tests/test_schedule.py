import io
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from laminator import actions, assets, schedule
from laminator.actions import Busy
from laminator.config import ConfigError
from laminator.mdblist import pick_score, score_pct, score_text
from laminator.schedule import Schedule
from laminator.store import Store
from laminator.web.scheduler import Scheduler


# ---- rounding: read and rounded to the first decimal --------------------------------------------------
def test_scores_are_rounded_half_up_to_one_decimal():
    assert [score_pct(x) for x in (73, 73.4, 73.49, 73.5, 73.6, 0.04, 99.5)] == [73, 73, 73, 74, 74, 0, 100]
    assert score_text(73) == "7.3" and score_text(100) == "10.0" and score_text(73, decimals=0) == "73"
    data = {"score": 73.4, "ratings": []}
    assert pick_score(data, ["aggregate"]) == ("aggregate", "7.3", 73)       # the same rounded value feeds poster and CriticRating


# ---- schedule config -------------------------------------------------------------------------------------
def test_schedule_defaults_and_validation():
    s = schedule.parse({})
    assert (s.changed_every_minutes, s.maintenance_day, s.maintenance_time) == (0, None, "03:00")      # nothing runs on its own until the user turns it on
    off = schedule.parse({"schedule": {"changed_every_minutes": 0, "maintenance": "off"}})
    assert off.changed_every_minutes == 0 and off.maintenance_day is None
    for bad in ({"changed_every_minutes": 3}, {"changed_every_minutes": "x"}, {"maintenance": {"day": 31}},
                {"maintenance": {"time": "25:00"}}, {"maintenance": 5}):
        with pytest.raises(ConfigError):
            schedule.parse({"schedule": bad})


def ts(*a):
    return datetime(*a).timestamp()


def test_occurrences_across_month_and_year_boundaries():
    assert schedule.last_occurrence(datetime(2026, 11, 1, 3, 0), 1, "03:00") == datetime(2026, 11, 1, 3, 0)
    assert schedule.last_occurrence(datetime(2026, 11, 1, 2, 59), 1, "03:00") == datetime(2026, 10, 1, 3, 0)
    assert schedule.last_occurrence(datetime(2027, 1, 1, 0, 0), 1, "03:00") == datetime(2026, 12, 1, 3, 0)
    assert schedule.next_occurrence(datetime(2026, 12, 15), 1, "03:00") == datetime(2027, 1, 1, 3, 0)
    assert schedule.next_occurrence(datetime(2026, 1, 28, 12), 28, "03:00") == datetime(2026, 2, 28, 3, 0)


def test_maintenance_due_catches_missed_runs(tmp_path):
    schedule.update(tmp_path, scheduled=True)
    s = Schedule(maintenance_day=1)
    assert schedule.decide(tmp_path, s, ts(2026, 11, 10)) == "baseline"            # first start: baseline, no immediate pass
    assert schedule.load(tmp_path)["last_full"] == ts(2026, 11, 10)
    assert schedule.decide(tmp_path, Schedule(changed_every_minutes=0, maintenance_day=1), ts(2026, 11, 20)) is None
    assert schedule.decide(tmp_path, Schedule(changed_every_minutes=0, maintenance_day=1), ts(2026, 12, 1, 3, 1)) == "maintenance"
    schedule.mark(tmp_path, "full", now=ts(2026, 12, 1, 3, 5))
    assert schedule.decide(tmp_path, Schedule(changed_every_minutes=0, maintenance_day=1), ts(2026, 12, 20)) is None
    # the machine was off on Jan 1 and Feb 1: still exactly one catch-up run, not two
    assert schedule.decide(tmp_path, Schedule(changed_every_minutes=0, maintenance_day=1), ts(2027, 2, 5)) == "maintenance"
    schedule.mark(tmp_path, "full", now=ts(2027, 2, 5, 1))
    assert schedule.decide(tmp_path, Schedule(changed_every_minutes=0, maintenance_day=1), ts(2027, 2, 6)) is None


def test_changed_check_interval_and_maintenance_priority(tmp_path):
    schedule.update(tmp_path, scheduled=True)
    s = Schedule(changed_every_minutes=30, maintenance_day=1)
    schedule.mark(tmp_path, "full", "changed", now=ts(2026, 11, 10, 12, 0))
    assert schedule.decide(tmp_path, s, ts(2026, 11, 10, 12, 29)) is None
    assert schedule.decide(tmp_path, s, ts(2026, 11, 10, 12, 30)) == "changed"
    schedule.mark(tmp_path, "changed", now=ts(2026, 12, 1, 3, 0))
    assert schedule.decide(tmp_path, s, ts(2026, 12, 1, 3, 1)) == "maintenance"     # a due maintenance wins over a check
    assert schedule.decide(tmp_path, Schedule(maintenance_day=None, changed_every_minutes=0), ts(2030, 1, 1)) is None


def test_scheduler_tick_starts_the_right_run_for_the_right_server_and_skips_when_busy(tmp_path):
    started = []
    (tmp_path / "emby").mkdir(); (tmp_path / "jellyfin").mkdir()
    schedule.update(tmp_path / "emby", scheduled=True); schedule.update(tmp_path / "jellyfin", scheduled=True)
    sch = Scheduler(Schedule(changed_every_minutes=30, maintenance_day=1), lambda: {"emby": tmp_path / "emby", "jellyfin": tmp_path / "jellyfin"},
                    lambda kind, what: started.append((kind, what)))
    assert sch.tick(ts(2026, 11, 10)) == "baseline" and started == []             # each server gets its own baseline
    assert schedule.load(tmp_path / "emby")["last_full"] and schedule.load(tmp_path / "jellyfin")["last_full"]
    assert sch.tick(ts(2026, 11, 10, 1)) == "changed" and started == [("emby", "changed")]      # one job per tick, never two at once
    schedule.mark(tmp_path / "emby", "changed", now=ts(2026, 11, 10, 1))
    assert sch.tick(ts(2026, 11, 10, 1, 1)) == "changed" and started[-1] == ("jellyfin", "changed")   # then the other one's turn
    sch.start_run = lambda kind, what: (_ for _ in ()).throw(Busy("another job"))
    assert sch.tick(ts(2026, 11, 10, 3)) == "busy"                                    # skipped, retried next tick


# ---- score drift: a change of 0.1 updates poster AND CriticRating together --------------------------------
def jpeg():
    b = io.BytesIO(); Image.new("RGB", (200, 300), (10, 10, 10)).save(b, "JPEG"); return b.getvalue()


class Srv:
    def __init__(self):
        self.it = NS(id="1", name="Elf", type="Movie", year=2003, provider_ids={"imdb": "tt1"}, has_poster=True, poster_tag="t")
        self.poster, self.uploads, self.posts = jpeg(), 0, []
        self.full = {"Id": "1", "Name": "Elf", "CriticRating": None, "LockedFields": []}

    def libraries(self): return [{"Name": "Movies", "CollectionType": "movies"}]
    def items(self, lib=None, limit=None): return [self.it]
    def download_primary(self, i, max_width=None): return self.poster, "image/jpeg"
    def upload_primary(self, i, data, ctype="image/jpeg"): self.uploads += 1; self.poster = data
    def item(self, i): return self.it
    def get_full(self, i): return dict(self.full)
    def post_item(self, i, body): self.posts.append(body["CriticRating"]); self.full = dict(body)


class Mdb:
    remaining = None
    score = 73

    def lookup(self, *a): return {"score": self.score, "ratings": []}


def full_pass(tmp_path, srv, mdb, only_changed=False, library=None):
    ws = actions.Workspace({}, NS(library=library, kind="emby"), srv, Store(tmp_path / "s.db"), mdb, tmp_path)
    tpl, sig = assets.load_template("default", tmp_path)
    return list(actions.apply_posters(ws, tpl, sig, tmp_path / "none.yaml", False, critic=True, only_changed=only_changed))


def test_only_a_tenth_of_a_point_change_updates_poster_and_critic_rating(tmp_path):
    srv, mdb = Srv(), Mdb()
    ev = full_pass(tmp_path, srv, mdb)
    assert [e.kind for e in ev if e.kind in ("apply", "critic")] == ["critic", "apply"] and srv.posts == [73] and srv.uploads == 1

    mdb.score = 73.4                                   # drifts, but still reads 7.3
    ev = full_pass(tmp_path, srv, mdb)
    assert [e.kind for e in ev if e.kind != "info"] == ["same"] and srv.uploads == 1 and srv.posts == [73]

    mdb.score = 74                                     # 8.1 -> 8.2 style step: now it counts
    ev = full_pass(tmp_path, srv, mdb)
    msgs = {e.kind: e.message for e in ev}
    assert "7.3 -> 7.4" in msgs["apply"] and "73 -> 74" in msgs["critic"]
    assert srv.uploads == 2 and srv.posts == [73, 74]
    s = actions.summarize(ev)
    assert "1 poster(s) updated (1 because the score changed)" in s.message and "1 critic rating(s) updated" in s.message

    mdb.score = 73.6                                   # and back down by a tenth (rounds to 7.4: no change)
    assert [e.kind for e in full_pass(tmp_path, srv, mdb) if e.kind != "info"] == ["same"]
    mdb.score = 72.5                                   # 7.25 rounds half up to 7.3 (73): a real 0.1 drop
    ev = full_pass(tmp_path, srv, mdb)
    assert srv.posts[-1] == 73 and "7.4 -> 7.3" in {e.kind: e.message for e in ev}["apply"]


def test_runs_over_every_library_are_recorded_for_the_schedule(tmp_path):
    srv, mdb = Srv(), Mdb()
    full_pass(tmp_path, srv, mdb, only_changed=True)
    st = schedule.load(tmp_path)
    assert "last_changed" in st and "last_full" not in st                    # a cheap check is not a maintenance pass
    full_pass(tmp_path, srv, mdb)
    assert "last_full" in schedule.load(tmp_path)
    (tmp_path / schedule.FILE).unlink()
    full_pass(tmp_path, srv, mdb, library="Movies")                           # a single-library run says nothing about "everything"
    assert schedule.load(tmp_path) == {}


def test_a_fresh_install_runs_nothing_on_its_own(tmp_path):
    s = schedule.parse({})
    assert s.changed_every_minutes == 0 and s.maintenance_day is None
    started = []
    (tmp_path / "emby").mkdir()
    sch = Scheduler(s, lambda: {"emby": tmp_path / "emby"}, lambda kind, what: started.append((kind, what)))
    for hour in range(0, 72, 6):                                                    # three days of ticks
        sch.tick(ts(2026, 11, 10) + hour * 3600)
    assert not [x for x in started if x[1] in ("changed", "maintenance")]            # no poster run is ever started


def test_a_zero_from_mdblist_means_no_score_not_a_badge_reading_0_0():
    assert pick_score({"score": 0, "ratings": []}, ["aggregate"]) is None
    assert pick_score({"score": 0, "ratings": [{"source": "imdb", "score": 0}]}, ["imdb", "aggregate"]) is None
    assert pick_score({"score": 0, "ratings": [{"source": "imdb", "score": 74}]}, ["aggregate", "imdb"]) == ("imdb", "7.4", 74)   # a real source still counts


# ---- a newly connected server stays out of scheduled runs until you include it ------------------------------------------------------
def test_a_server_without_state_is_not_scheduled_until_included(tmp_path):
    folder = tmp_path / "jellyfin"; folder.mkdir()
    s = Schedule(changed_every_minutes=30, maintenance_day=1)
    assert not schedule.scheduled_on(folder) and schedule.decide(folder, s, ts(2026, 11, 10)) is None and schedule.load(folder) == {}   # not even a baseline
    schedule.update(folder, scheduled=True)
    assert schedule.scheduled_on(folder) and schedule.decide(folder, s, ts(2026, 11, 10)) == "baseline"
    schedule.update(folder, scheduled=False)
    assert schedule.decide(folder, s, ts(2026, 12, 2)) is None


def test_a_server_that_already_has_saved_state_keeps_being_scheduled(tmp_path):
    folder = tmp_path / "emby"; folder.mkdir()
    st = Store(folder / "state.db"); st.put_original("1", "Elf", str(folder / "1.img"), "sha", "tag"); st.db.close()
    assert schedule.scheduled_on(folder)                                                  # the upgrade changes nothing for it
    schedule.update(folder, scheduled=False)
    assert not schedule.scheduled_on(folder)                                              # until you switch it off


def test_the_tick_skips_a_server_that_is_not_included(tmp_path):
    (tmp_path / "emby").mkdir(); (tmp_path / "jellyfin").mkdir()
    schedule.update(tmp_path / "emby", scheduled=True)
    started = []
    sch = Scheduler(Schedule(changed_every_minutes=30, maintenance_day=1), lambda: {"jellyfin": tmp_path / "jellyfin", "emby": tmp_path / "emby"},
                    lambda kind, what: started.append((kind, what)) or None)
    sch.tick(ts(2026, 11, 10))
    assert schedule.load(tmp_path / "jellyfin") == {} and schedule.load(tmp_path / "emby").get("last_full")
