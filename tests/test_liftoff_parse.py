import copy
import json
from pathlib import Path
from typing import Any

import pytest

from orbit.integrations.liftoff_parse import (
    LiftoffParseError,
    parse_duration,
    parse_post,
    pounds_to_grams,
)

POSTS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "liftoff" / "post_getMyPosts.json").read_text()
)


def post(post_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(p for p in POSTS if p["id"] == post_id))


def test_duration_text() -> None:
    assert parse_duration("01 hours 06 minutes 01 seconds") == 3961
    assert parse_duration("289 hours 27 minutes 36 seconds") == 1042056
    assert parse_duration("00 hours 00 minutes 00 seconds") == 0
    with pytest.raises(LiftoffParseError):
        parse_duration("about an hour")


def test_pounds_become_grams() -> None:
    assert pounds_to_grams(66.1386786555) == 30000  # 30 kg
    assert pounds_to_grams(165.34669663875) == 75000


def test_a_workout_with_weights_reps_and_notes() -> None:
    parsed = parse_post(post("post-001"))
    assert parsed.liftoff_id == "post-001"
    assert parsed.started_at.isoformat() == "2026-02-08T17:12:41.318000+00:00"
    assert parsed.duration_seconds == 4328
    assert not parsed.duration_suspect
    assert parsed.bodyweight_grams == 69989  # 154.3 lb
    assert parsed.notes == "Good session, shoulders a bit tired."
    assert len(parsed.exercises) == 8
    first = parsed.exercises[0]
    assert (first.kind, first.name) == ("WR", "Cable Lateral Raise")
    assert [s.reps for s in first.sets] == [13, 5, 3, 7]
    assert first.sets[0].weight_grams == pounds_to_grams(11.0231)  # 5 kg


def test_distance_is_km_and_an_impossible_pace_is_flagged() -> None:
    sets = parse_post(post("post-001")).exercises[7].sets  # 3.99921984 km in 120 s, seven times
    assert {s.distance_m for s in sets} == {3999}
    assert {s.seconds for s in sets} == {120}
    assert all(s.implausible for s in sets)
    assert all(s.weight_grams is None and s.reps is None for s in sets)


def test_a_believable_run_is_not_flagged() -> None:
    data = post("post-001")
    data["exerciseData"][7]["setsData"] = [
        {"setIndex": 0, "setType": "normal", "inputOne": 6.5983104, "inputTwo": 2700}
    ]  # 4.1 miles in 45 minutes
    run = parse_post(data).exercises[7].sets[0]
    assert (run.distance_m, run.seconds, run.implausible) == (6598, 2700, False)


def test_bodyweight_reps_have_no_weight_and_drop_sets_are_kept() -> None:
    parsed = parse_post(post("post-003"))
    bodyweight = next(e for e in parsed.exercises if e.kind == "BR")
    assert all(s.weight_grams is None and s.reps for s in bodyweight.sets)
    assert "drop" in {s.set_type for e in parsed.exercises for s in e.sets}


def test_assisted_exercise_keeps_its_weight() -> None:
    parsed = parse_post(post("post-002"))
    assisted = next(e for e in parsed.exercises if e.kind == "AB")
    assert any(s.weight_grams for s in assisted.sets)


def test_a_session_left_open_is_suspect() -> None:
    data = post("post-004")
    data["sessionDuration"] = "95 hours 37 minutes 33 seconds"
    parsed = parse_post(data)
    assert parsed.duration_seconds == 344253
    assert parsed.duration_suspect


def test_missing_or_odd_bodyweight_is_none() -> None:
    for value in ("", None, "n/a", "0"):
        data = post("post-004")
        data["bodyweight"] = value
        assert parse_post(data).bodyweight_grams is None


def test_inputs_may_be_text_numbers_or_missing() -> None:
    data = post("post-004")
    data["exerciseData"][0]["setsData"] = [
        {"setType": "normal", "inputOne": "0", "inputTwo": "12"},
        {"setType": "normal", "inputOne": None, "inputTwo": 8},
    ]
    assert [s.reps for s in parse_post(data).exercises[0].sets] == [12, 8]


@pytest.mark.parametrize("field", ["id", "startedAt", "sessionDuration"])
def test_a_post_without_a_required_field_is_rejected(field: str) -> None:
    data = post("post-004")
    del data[field]
    with pytest.raises(LiftoffParseError, match=field):
        parse_post(data)


def test_unknown_fields_are_ignored_and_the_hash_follows_content() -> None:
    data = post("post-004")
    data["somethingNew"] = {"x": 1}
    assert parse_post(data).content_hash == parse_post(post("post-004")).content_hash
    data["exerciseData"][0]["setsData"][0]["inputTwo"] = 99
    assert parse_post(data).content_hash != parse_post(post("post-004")).content_hash
