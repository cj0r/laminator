import json
from types import SimpleNamespace as NS

import pytest

from laminator import meta
from laminator.meta import OverrideError, plan_item, resolve

FULL = {"Id": "8", "Name": "Elf", "OfficialRating": "PG", "ForcedSortName": "Elf", "LockedFields": []}


def row(old, ovr, lock_added=1):
    return {"old_value": json.dumps(old), "override_value": json.dumps(ovr), "lock_added": lock_added}


def acts(plan):
    return {c.key: c.action for c in plan.changes}


def test_fresh_override_is_a_change():
    p = plan_item(FULL, {"rating": "PG-13"}, {})
    assert acts(p) == {"rating": "change"} and p.writes


def test_already_set_but_unlocked_only_locks():
    f = dict(FULL, OfficialRating="PG-13")
    assert acts(plan_item(f, {"rating": "PG-13"}, {})) == {"rating": "lock"}
    f["LockedFields"] = ["OfficialRating"]
    assert acts(plan_item(f, {"rating": "PG-13"}, {})) == {"rating": "ok"}


def test_server_reverted_is_reapplied():
    assert acts(plan_item(FULL, {"rating": "PG-13"}, {"rating": row("PG", "PG-13")})) == {"rating": "reapply"}


def test_override_edited_in_file_is_a_change():
    f = dict(FULL, OfficialRating="PG-13")
    assert acts(plan_item(f, {"rating": "R"}, {"rating": row("PG", "PG-13")})) == {"rating": "change"}


def test_hand_edit_is_detected_and_not_written():
    f = dict(FULL, OfficialRating="R")
    p = plan_item(f, {"rating": "PG-13"}, {"rating": row("PG", "PG-13")})
    assert acts(p) == {"rating": "hand-edit"} and not p.writes


def test_unknown_rating_rejected():
    p = plan_item(FULL, {"rating": "PG-14"}, {}, valid_ratings=["PG", "PG-13"])
    assert p.error and "PG-14" in p.error


def test_sort_title_locks_sortname():
    assert meta.FIELDS["sort_title"] == ("ForcedSortName", "SortName")


def items():
    mk = lambda i, n, y, **ids: NS(id=i, name=n, year=y, provider_ids=ids)
    return {"Movies": [mk("1", "Dumbo", 1941, imdb="tt1"), mk("2", "Elf", 2003, imdb="TT2")],
            "Kids": [mk("3", "Elf", 2003, imdb="tt3")]}


def test_resolve_by_id_case_insensitive_and_by_title():
    assert [i.id for i in resolve({"match": {"imdb": "tt2"}}, items())] == ["2"]
    assert [i.id for i in resolve({"match": {"library": "Movies", "title": "elf", "year": 2003}}, items())] == ["2"]


def test_a_title_in_an_hd_and_a_uhd_library_matches_both_copies_unless_narrowed():
    both = resolve({"match": {"title": "Elf"}}, items())
    assert sorted(i.id for i in both) == ["2", "3"]
    assert [i.id for i in resolve({"match": {"title": "Elf", "library": "Kids"}}, items())] == ["3"]
    same_id = {"HD": [NS(id="a", name="X", year=1, provider_ids={"imdb": "tt9"})], "UHD": [NS(id="b", name="X", year=1, provider_ids={"imdb": "tt9"})]}
    assert sorted(i.id for i in resolve({"match": {"imdb": "tt9"}}, same_id)) == ["a", "b"]
    out, errs = meta.score_overrides([{"match": {"imdb": "tt9"}, "score": 7}], same_id)
    assert out == {"a": 7, "b": 7} and not errs
    per, errs = meta.per_item_settings([{"match": {"imdb": "tt9"}, "overlay": False}], same_id)
    assert per == {"a": {"overlay": False}, "b": {"overlay": False}} and not errs


def test_resolve_missing():
    with pytest.raises(OverrideError):
        resolve({"match": {"imdb": "nope"}}, items())


def test_load_overrides_validates(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    colour: red\n")
    with pytest.raises(OverrideError):
        meta.load_overrides(f)
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    rating: PG-13\n")
    assert meta.load_overrides(f)[0]["rating"] == "PG-13"


def test_score_checks_and_format():
    for bad in (11, -1, True, "", "toolongtext", None, [8]):
        with pytest.raises(OverrideError):
            meta.check_score(bad)
    for ok in (0, 8.5, 10, "A+"):
        meta.check_score(ok)
    assert meta.format_score(8.5) == "8.5" and meta.format_score(8) == "8.0"
    assert meta.format_score(8.5, decimals=0) == "85" and meta.format_score(" A+ ") == "A+"


def test_score_overrides_resolve_and_report_errors():
    entries = [{"match": {"imdb": "tt2"}, "score": 9},
               {"match": {"title": "Nope"}, "score": 1},         # matches nothing
               {"match": {"imdb": "tt1"}, "rating": "R"}]        # no score: ignored here
    out, errs = meta.score_overrides(entries, items())
    assert out == {"2": 9} and len(errs) == 1


def test_score_only_entry_is_valid_and_planned_nowhere(tmp_path):
    f = tmp_path / "o.yaml"
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    score: 7.7\n")
    assert meta.load_overrides(f)[0]["score"] == 7.7
    assert meta.load_overrides(tmp_path / "missing.yaml", required=False) == []


def test_critic_rating_is_a_number_the_user_may_set_by_hand(tmp_path):
    assert "critic_rating" not in meta.FIELDS and meta._ALL["critic_rating"] == ("CriticRating", "CriticRating")
    f = tmp_path / "o.yaml"
    f.write_text("overrides:\n  - match: {imdb: tt1}\n    critic_rating: 80\n")
    assert meta.load_overrides(f)[0]["critic_rating"] == 80
    for bad in ("80.5", "101", "-1", "true", "high"):
        f.write_text(f"overrides:\n  - match: {{imdb: tt1}}\n    critic_rating: {bad}\n")
        with pytest.raises(OverrideError, match="whole number from 0 to 100"):
            meta.load_overrides(f)
    full = {"Id": "1", "Name": "X", "CriticRating": None, "LockedFields": []}
    assert acts(plan_item(full, {"critic_rating": 73}, {})) == {"critic_rating": "change"}
    full.update(CriticRating=73.0, LockedFields=["CriticRating"])
    assert acts(plan_item(full, {"critic_rating": 73}, {})) == {"critic_rating": "ok"}   # 73.0 == 73
    full["CriticRating"] = 50
    assert acts(plan_item(full, {"critic_rating": 73}, {"critic_rating": row(None, 73)})) == {"critic_rating": "hand-edit"}
