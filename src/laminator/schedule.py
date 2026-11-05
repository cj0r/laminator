"""When did laminator last run, and what is due. Pure functions plus a tiny JSON state file in the data folder.

Two kinds of run:
  changed      cheap: new items and items whose poster changed on the server (apply --changed)
  maintenance  monthly full pass over every library: picks up score changes (a change of 0.1 or more in the
               rounded score), template and override edits; updates the poster and CriticRating together
"""
from __future__ import annotations

import calendar
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import ConfigError

FILE = "schedule.json"
DAY = 86400


@dataclass
class Schedule:
    changed_every_minutes: int = 0        # 0 = off (the default: nothing is applied on its own until you turn it on)
    maintenance_day: int | None = None    # day of the month; None = off (the default)
    maintenance_time: str = "03:00"       # local time, HH:MM
    template: str = "default"
    backup_every_days: int = 7            # 0 = off; a backup of laminator's own data (no secrets: there is no passphrase to encrypt them with)
    backup_keep: int = 5                  # how many scheduled backups to keep (hand-made and uploaded ones are never pruned)


def parse(cfg: dict) -> Schedule:
    s = cfg.get("schedule") or {}
    if not isinstance(s, dict):
        raise ConfigError("schedule: must be a mapping")
    out = Schedule(template=str(s.get("template", "default")))
    m = s.get("changed_every_minutes", 0)
    if not isinstance(m, int) or isinstance(m, bool) or not (m == 0 or 5 <= m <= 1440):
        raise ConfigError("schedule.changed_every_minutes must be 0 (off) or 5-1440")
    out.changed_every_minutes = m
    maint = s.get("maintenance", "off")
    if maint in (False, None, "off"):
        out.maintenance_day = None
    else:
        if not isinstance(maint, dict):
            raise ConfigError("schedule.maintenance must be 'off' or {day: 1-28, time: 'HH:MM'}")
        day = maint.get("day", 1)
        if not isinstance(day, int) or isinstance(day, bool) or not 1 <= day <= 28:
            raise ConfigError("schedule.maintenance.day must be 1-28 (so every month has it)")
        t = str(maint.get("time", "03:00"))
        try:
            datetime.strptime(t, "%H:%M")
        except ValueError:
            raise ConfigError("schedule.maintenance.time must look like 03:00")
        out.maintenance_day, out.maintenance_time = day, t
    b = s.get("backup", {})
    if b in (False, None, "off"):
        out.backup_every_days = 0
    else:
        if not isinstance(b, dict):
            raise ConfigError("schedule.backup must be 'off' or {every_days: 0-90, keep: 1-50}")
        for key, lo, hi, attr in (("every_days", 0, 90, "backup_every_days"), ("keep", 1, 50, "backup_keep")):
            v = b.get(key, getattr(out, attr))
            if not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi:
                raise ConfigError(f"schedule.backup.{key} must be a whole number from {lo} to {hi}")
            setattr(out, attr, v)
    return out


# ---- state ------------------------------------------------------------------------------------
def load(data: Path) -> dict:
    try:
        return json.loads((data / FILE).read_text())
    except (FileNotFoundError, ValueError):
        return {}


def mark(data: Path, *kinds: str, now: float | None = None) -> None:
    """Record that a run of these kinds just completed ('full', 'changed')."""
    st = load(data)
    for k in kinds:
        st[f"last_{k}"] = now or time.time()
    tmp = data / (FILE + ".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(data / FILE)


# ---- due logic ----------------------------------------------------------------------------------
def occurrence(year: int, month: int, day: int, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime(year, month, min(day, calendar.monthrange(year, month)[1]), h, m)


def last_occurrence(now: datetime, day: int, hhmm: str) -> datetime:
    """The most recent scheduled maintenance time that is <= now."""
    this = occurrence(now.year, now.month, day, hhmm)
    if this <= now:
        return this
    prev = (now.replace(day=1) - timedelta(days=1))
    return occurrence(prev.year, prev.month, day, hhmm)


def next_occurrence(now: datetime, day: int, hhmm: str) -> datetime:
    last = last_occurrence(now, day, hhmm)
    nxt = (last.replace(day=1) + timedelta(days=32)).replace(day=1)
    return occurrence(nxt.year, nxt.month, day, hhmm)


def maintenance_due(state: dict, sched: Schedule, now: float) -> bool:
    """True when a scheduled time has passed since the last full pass (also catches a missed run after downtime).
    With no record at all the caller should baseline instead (see decide)."""
    if sched.maintenance_day is None or state.get("last_full") is None:
        return False
    due = last_occurrence(datetime.fromtimestamp(now), sched.maintenance_day, sched.maintenance_time)
    return state["last_full"] < due.timestamp()


def changed_due(state: dict, sched: Schedule, now: float) -> bool:
    return sched.changed_every_minutes > 0 and now - state.get("last_changed", 0) >= sched.changed_every_minutes * 60


def update(data: Path, **values) -> None:
    """Record arbitrary small facts next to the run times (a flag, an error sentence)."""
    st = load(data)
    st.update(values)
    tmp = data / (FILE + ".tmp")
    tmp.write_text(json.dumps(st))
    tmp.replace(data / FILE)


def backup_due(root: Path, sched: Schedule, now: float) -> bool:
    """A scheduled backup is due when the last good one is older than the interval (or there is none), and the last attempt (good or
    not) was over an hour ago, so a persistent failure retries hourly instead of every tick."""
    st = load(root)
    last, tried = st.get("last_backup"), st.get("last_backup_try", 0)
    if now - tried < 3600:
        return False
    if st.get("push_pending"):  # a backup exists locally but its copy to the destination failed: try the copy again
        return True
    return sched.backup_every_days > 0 and (last is None or now - last >= sched.backup_every_days * DAY)


def scheduled_on(folder: Path) -> bool:
    """Is this server part of the scheduled runs? A server that already has saved state keeps being scheduled, as before. A newly connected
    one is not until you tick "Include in scheduled runs" in Settings: its first scheduled check would otherwise treat every title as new
    and draw a badge on the whole library without being asked."""
    v = load(folder).get("scheduled")
    if isinstance(v, bool):
        return v
    from .store import state_counts
    return sum(state_counts(folder)) > 0


def decide(data: Path, sched: Schedule, now: float | None = None) -> str | None:
    """'maintenance', 'changed', 'baseline' or None. 'baseline' records 'now' as the last full pass the first time
    (a new install has just done its first full pass; do not run another one immediately)."""
    now = now or time.time()
    if not scheduled_on(data):
        return None
    st = load(data)
    if sched.maintenance_day is not None and st.get("last_full") is None:
        mark(data, "full", now=now)
        return "baseline"
    if maintenance_due(st, sched, now):
        return "maintenance"
    if changed_due(st, sched, now):
        return "changed"
    return None
