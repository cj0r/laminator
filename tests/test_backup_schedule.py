import os
import re
import time
import tarfile

import pytest
from fastapi.testclient import TestClient

from laminator import backup, schedule
from laminator import schedule as schedmod
from laminator.actions import Busy
from laminator.backup import BackupError
from laminator.config import ConfigError
from laminator.schedule import Schedule
from laminator.web.app import create_app
from laminator.web.scheduler import Scheduler
from test_backup import make_root, names
from test_web import FakeMdb, FakeServer, finished, login

DAY = 86400
T0 = 1_800_000_000.0


def test_defaults_and_validation():
    s = schedule.parse({})
    assert (s.backup_every_days, s.backup_keep) == (7, 5)
    assert schedule.parse({"schedule": {"backup": "off"}}).backup_every_days == 0
    assert schedule.parse({"schedule": {"backup": {"every_days": 1, "keep": 10}}}).backup_keep == 10
    for bad in ({"backup": {"every_days": -1}}, {"backup": {"every_days": 91}}, {"backup": {"keep": 0}}, {"backup": {"keep": 51}},
                {"backup": {"keep": "5"}}, {"backup": {"every_days": True}}, {"backup": 3}):
        with pytest.raises(ConfigError):
            schedule.parse({"schedule": bad})


def test_when_a_backup_is_due(tmp_path):
    s = Schedule(backup_every_days=7)
    assert schedule.backup_due(tmp_path, s, T0)                                           # never made one: due
    schedule.mark(tmp_path, "backup", now=T0)
    assert not schedule.backup_due(tmp_path, s, T0 + 6 * DAY) and schedule.backup_due(tmp_path, s, T0 + 7 * DAY)
    schedule.mark(tmp_path, "backup_try", now=T0 + 7 * DAY)
    assert not schedule.backup_due(tmp_path, s, T0 + 7 * DAY + 1800)                      # tried half an hour ago: not again yet
    assert schedule.backup_due(tmp_path, s, T0 + 7 * DAY + 3601)                          # a failing backup is retried hourly
    assert not schedule.backup_due(tmp_path, Schedule(backup_every_days=0), T0 + 99 * DAY)   # off


def test_the_scheduler_starts_a_backup_first_and_records_the_attempt_only_when_it_started(tmp_path):
    started = []
    sch = Scheduler(Schedule(changed_every_minutes=30), lambda: {}, lambda k, w: started.append((k, w)), root=tmp_path)
    assert sch.tick(T0) == "backup" and started == [(None, "backup")]
    assert sch.tick(T0 + 60) != "backup"                                                    # the attempt was recorded: not again within the hour
    sch2 = Scheduler(Schedule(), lambda: {}, lambda k, w: (_ for _ in ()).throw(Busy("busy")), root=tmp_path / "b")
    (tmp_path / "b").mkdir()
    assert sch2.tick(T0) == "busy" and "last_backup_try" not in schedule.load(tmp_path / "b")   # busy: nothing recorded, tried next tick


# ---- creating and pruning -------------------------------------------------------------------------------------------
@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path / "data")


def autos(root): return sorted(f.name for f in (root / "backups").glob("laminator-backup-auto-*.tar.gz"))


def test_auto_backups_are_verified_secret_free_and_pruned_to_the_newest_keep(root, monkeypatch):
    clock = iter(f"2026110{i}-120000" for i in range(1, 8))
    real = time.strftime
    monkeypatch.setattr(backup.time, "strftime", lambda fmt, *a: backup.AUTO_PREFIX + next(clock) + ".tar.gz" if fmt.startswith(backup.AUTO_PREFIX) else real(fmt, *a))
    for _ in range(5):
        backup.create_auto(root, keep=3)
    assert autos(root) == [f"laminator-backup-auto-2026110{i}-120000.tar.gz" for i in (3, 4, 5)]      # the 3 newest remain
    newest = root / "backups" / autos(root)[-1]
    assert "secrets.env" not in names(newest) and "secrets.enc" not in names(newest)
    assert backup.verify(newest).files > 0


def test_pruning_never_touches_backups_you_made_or_uploaded(root):
    bd = root / "backups"; bd.mkdir(exist_ok=True)
    mine = backup.create(root, bd / "laminator-backup-20200101-000000.tar.gz").path
    uploaded = bd / "laminator-backup-uploaded-20200202-000000.tar.gz"; uploaded.write_bytes(mine.read_bytes())
    (bd / "laminator-backup-auto-20200303-000000.tar.gz").write_bytes(mine.read_bytes())
    (bd / "notes.txt").write_text("keep me"); (bd / "laminator-backup-auto-evil.tar.gz.part").write_bytes(b"x")
    outside = root.parent / "outside.tar.gz"; outside.write_bytes(b"precious")
    (bd / "laminator-backup-auto-20190101-000000.tar.gz").symlink_to(outside)
    rep, pruned = backup.create_auto(root, keep=1)
    assert pruned == ["laminator-backup-auto-20200303-000000.tar.gz"]                               # the older auto one only
    assert mine.exists() and uploaded.exists() and (bd / "notes.txt").exists() and outside.read_bytes() == b"precious"
    assert (bd / "laminator-backup-auto-20190101-000000.tar.gz").is_symlink() and (bd / "laminator-backup-auto-evil.tar.gz.part").exists()


def test_a_new_backup_that_does_not_verify_is_deleted_and_nothing_older_is_pruned(root, monkeypatch):
    backup.create_auto(root, keep=1)
    before = autos(root)
    os.rename(root / "backups" / before[0], root / "backups" / "laminator-backup-auto-20200101-000000.tar.gz")
    def bad_verify(path, passphrase=None): raise BackupError("checksum mismatch")
    monkeypatch.setattr(backup, "verify", bad_verify)
    with pytest.raises(BackupError, match="checksum"):
        backup.create_auto(root, keep=1)
    assert autos(root) == ["laminator-backup-auto-20200101-000000.tar.gz"]                         # the good old one survived; the bad new one is gone


def test_keep_is_bounded(root):
    for bad in (0, -1, 51):
        with pytest.raises(BackupError):
            backup.create_auto(root, keep=bad)


# ---- through the web app -----------------------------------------------------------------------------------------------
def make_ui(tmp_path, monkeypatch):
    for n in ("API_EMBY", "API_MDBLIST"):
        monkeypatch.delenv(n, raising=False)
    r = make_root(tmp_path / "data"); (r / "admin.hash").unlink()
    app = create_app({"servers": {"emby": {"url": "http://x"}}}, {"emby": FakeServer()}, r, "pw", FakeMdb(), sched=Schedule(backup_every_days=7, backup_keep=2))
    c = TestClient(app, follow_redirects=False); login(c)
    return app, c, r, lambda: re.search(r'name="token" value="([0-9a-f]+)"', c.get("/settings").text).group(1)


def test_a_scheduled_backup_runs_as_a_job_marks_itself_and_prunes(tmp_path, monkeypatch):
    app, c, r, tok = make_ui(tmp_path, monkeypatch)
    bd = r / "backups"; bd.mkdir()
    for d in (1, 2, 3):
        (bd / f"laminator-backup-auto-2020010{d}-000000.tar.gz").write_bytes(backup.create(r, bd / f"x{d}.tar.gz").path.read_bytes())
        (bd / f"x{d}.tar.gz").unlink()
    app.state.scheduler.start_run(None, "backup")
    jid = re.findall(r"/jobs/(?!table)([\w-]+)", c.get("/jobs").text)[0]
    log = finished(app, c, f"/jobs/{jid}")
    assert "verified" in log and "not included" in log and "removed 2 older scheduled backup" in log and "done" in log
    assert len(autos(r)) == 2 and schedule.load(r).get("last_backup")                              # keep=2 from the schedule; the time was recorded
    assert "backup: every 7 day(s), keep the last 2" in c.get("/status").text


def test_backup_settings_are_saved_and_validated(tmp_path, monkeypatch):
    app, c, r, tok = make_ui(tmp_path, monkeypatch)
    body = {"changed_every_minutes": "30", "maint_on": "1", "maint_day": "1", "maint_time": "03:00", "sched_template": "default",
            "grace_days": "30", "trash_days": "90", "token": tok()}
    assert "note=genok" in c.post("/settings/general", data={**body, "backup_every_days": "2", "backup_keep": "9"}).headers["location"]
    assert app.state.scheduler.sched.backup_every_days == 2 and app.state.scheduler.sched.backup_keep == 9
    assert 'value="9"' in c.get("/settings").text
    for bad in ({"backup_every_days": "-1"}, {"backup_every_days": "91"}, {"backup_keep": "0"}, {"backup_keep": "x"}):
        r2 = c.post("/settings/general", data={**body, "backup_every_days": "2", "backup_keep": "9", **bad, "token": tok()})
        assert "note=bad" in r2.headers["location"], bad
    assert app.state.scheduler.sched.backup_keep == 9
    assert "note=genok" in c.post("/settings/general", data={**body, "backup_every_days": "0", "backup_keep": "3", "token": tok()}).headers["location"]
    assert app.state.scheduler.sched.backup_every_days == 0


def test_the_backup_schedule_has_its_own_form_and_the_options_form_leaves_it_alone(tmp_path, monkeypatch):
    app, c, r, tok = make_ui(tmp_path, monkeypatch)
    page = c.get("/settings").text
    assert 'action="/settings/backup/schedule"' in page and page.count('name="backup_every_days"') == 1 and "Maintenance" in page and "About these two" not in page
    assert "note=genok" in c.post("/settings/backup/schedule", data={"backup_every_days": "3", "backup_keep": "4", "token": tok()}).headers["location"]
    s = app.state.scheduler.sched
    assert (s.backup_every_days, s.backup_keep, s.changed_every_minutes) == (3, 4, 0)       # the rest of the schedule is untouched
    body = {"changed_every_minutes": "60", "maint_on": "1", "maint_day": "2", "maint_time": "04:00", "sched_template": "default",
            "grace_days": "30", "trash_days": "90", "token": tok()}
    assert "note=genok" in c.post("/settings/general", data=body).headers["location"]          # no backup fields posted
    s = app.state.scheduler.sched
    assert (s.backup_every_days, s.backup_keep, s.changed_every_minutes, s.maintenance_day) == (3, 4, 60, 2)
    for bad in ({"backup_every_days": "-1"}, {"backup_every_days": "91"}, {"backup_keep": "0"}, {"backup_keep": "x"}):
        assert "note=bad" in c.post("/settings/backup/schedule", data={"backup_every_days": "3", "backup_keep": "4", **bad, "token": tok()}).headers["location"]
    assert c.post("/settings/backup/schedule", data={"backup_every_days": "3", "backup_keep": "4", "token": ""}).status_code == 403
