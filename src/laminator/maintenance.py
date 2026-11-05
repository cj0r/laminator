"""The full maintenance pass: re-read scores and update posters/CriticRating where the rounded score changed (apply_posters, full),
then clean up what is no longer needed. One definition, used by `laminator maintain`, the scheduler and the UI."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from . import actions, cleanup
from .actions import Event


def run(ws, tpl: dict, sig: str, overrides_path: Path, required: bool, critic: bool, do_cleanup: bool = True,
        limits: cleanup.Limits | None = None) -> Iterator[Event]:
    seen: list[Event] = []
    for e in actions.apply_posters(ws, tpl, sig, overrides_path, required, None, critic, False):
        seen.append(e)
        yield e
    yield actions.summarize(seen)
    if do_cleanup:
        yield from cleanup.run(ws, lim=limits)
