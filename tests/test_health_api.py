from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

TODAY = datetime.now(UTC).date().isoformat()
FULL = {
    "day": "2026-10-08",
    "sleep_minutes": 423,
    "sleep_start": "2026-10-07T22:41:00+02:00",
    "sleep_end": "2026-10-08T06:44:00+02:00",
    "resting_hr": 54,
    "avg_hr": 71,
    "min_hr": 48,
    "max_hr": 162,
    "steps": 9120,
    "active_kcal": 540,
    "weight_kg": 75.2,
}


def test_a_full_day_is_stored_and_returned(api: TestClient) -> None:
    response = api.post("/api/v1/health/daily", json=FULL)
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert (body["sleep_minutes"], body["resting_hr"], body["steps"]) == (423, 54, 9120)
    assert body["weight_kg"] == 75.2
    assert (body["min_hr"], body["max_hr"]) == (48, 162)
    assert body["sleep_start"] == "2026-10-07T20:41:00Z"  # stored in UTC


def test_posting_the_same_day_again_is_idempotent_and_keeps_other_fields(api: TestClient) -> None:
    api.post("/api/v1/health/daily", json=FULL)
    second = api.post(
        "/api/v1/health/daily", json={"day": "2026-10-08", "steps": 10000, "sleep_minutes": None}
    ).json()
    assert second["created"] is False
    assert second["steps"] == 10000
    assert second["sleep_minutes"] == 423  # null means "no data", not "erase"
    assert second["resting_hr"] == 54
    assert len(api.get("/api/v1/health/days?start=2026-10-01&end=2026-10-31").json()) == 1


def test_numbers_may_be_text_or_have_decimals(api: TestClient) -> None:
    body = api.post(
        "/api/v1/health/daily",
        json={"day": "2026-10-08", "steps": "8123.6", "resting_hr": "54.4", "weight_kg": "75"},
    ).json()
    assert (body["steps"], body["resting_hr"], body["weight_kg"]) == (8124, 54, 75.0)


def test_a_time_without_an_offset_is_read_in_the_configured_timezone(api: TestClient) -> None:
    body = api.post(
        "/api/v1/health/daily",
        json={"day": "2026-10-08", "sleep_start": "2026-10-07T22:41:00"},
    ).json()
    assert body["sleep_start"] == "2026-10-07T20:41:00Z"  # Europe/Prague, UTC+2 in October


def test_unknown_keys_are_ignored(api: TestClient) -> None:
    response = api.post(
        "/api/v1/health/daily", json={"day": "2026-10-08", "steps": 5, "mood": "good"}
    )
    assert response.status_code == 200


def test_impossible_values_are_rejected_and_nothing_is_saved(api: TestClient) -> None:
    for bad in (
        {"sleep_minutes": 2000},
        {"resting_hr": 5},
        {"min_hr": 5},
        {"max_hr": 300},
        {"steps": -1},
        {"weight_kg": 750},
        {"sleep_start": "2026-10-08T07:00:00+02:00", "sleep_end": "2026-10-07T23:00:00+02:00"},
    ):
        assert (
            api.post("/api/v1/health/daily", json={"day": "2026-10-08", **bad}).status_code == 422
        )
    future = (datetime.now(UTC) + timedelta(days=5)).date().isoformat()
    assert api.post("/api/v1/health/daily", json={"day": future}).status_code == 422
    assert api.get("/api/v1/health/days?start=2026-10-01&end=2026-10-31").json() == []


def test_a_missing_or_empty_day_means_today(api: TestClient) -> None:
    for body in ({"steps": 5}, {"day": "", "steps": 6}, {"day": None, "steps": 7}):
        response = api.post("/api/v1/health/daily", json=body)
        assert response.status_code == 200
        assert response.json()["day"] == datetime.now(ZoneInfo("Europe/Prague")).date().isoformat()
    assert len(api.get("/api/v1/health/days").json()) == 1  # all three were the same day


def test_an_empty_sleep_time_is_no_data(api: TestClient) -> None:
    body = api.post(
        "/api/v1/health/daily", json={"day": "2026-10-08", "sleep_start": "", "steps": 5}
    ).json()
    assert body["sleep_start"] is None and body["steps"] == 5


def test_a_garbage_day_is_still_rejected(api: TestClient) -> None:
    assert api.post("/api/v1/health/daily", json={"day": "x", "steps": 5}).status_code == 422


def test_list_is_oldest_first_and_bounded(api: TestClient) -> None:
    for day in ("2026-10-03", "2026-10-01", "2026-10-02"):
        api.post("/api/v1/health/daily", json={"day": day, "steps": 1})
    days = api.get("/api/v1/health/days?start=2026-10-01&end=2026-10-03").json()
    assert [d["day"] for d in days] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert api.get("/api/v1/health/days?start=2025-01-01&end=2026-10-03").status_code == 422
    assert api.get("/api/v1/health/days?start=2026-10-03&end=2026-10-01").status_code == 422
    assert isinstance(api.get("/api/v1/health/days").json(), list)


def test_the_token_is_required(client: TestClient) -> None:
    assert client.post("/api/v1/health/daily", json={"day": TODAY}).status_code == 401
    assert client.get("/api/v1/health/days").status_code == 401


def sleep_payload(**extra: object) -> dict[str, object]:
    import json
    from pathlib import Path

    sample = json.loads(
        (Path(__file__).parent / "fixtures" / "health" / "sleep_probe.json").read_text()
    )
    sample.pop("note")
    return {"day": "2026-10-09", **sample, **extra}


def test_raw_sleep_samples_become_a_night_with_stages(api: TestClient) -> None:
    body = api.post("/api/v1/health/daily", json=sleep_payload()).json()
    assert body["sleep_minutes"] == 443
    assert (body["sleep_deep_minutes"], body["sleep_core_minutes"]) == (62, 288)
    assert (body["sleep_rem_minutes"], body["sleep_awake_minutes"]) == (93, 21)
    assert body["sleep_in_bed_minutes"] == 540
    assert body["sleep_start"] == "2026-10-08T21:05:00Z"
    assert body["sleep_end"] == "2026-10-09T04:49:00Z"


def test_raw_sleep_wins_over_a_number_and_other_fields_are_kept(api: TestClient) -> None:
    api.post("/api/v1/health/daily", json={"day": "2026-10-09", "steps": 9000})
    body = api.post("/api/v1/health/daily", json=sleep_payload(sleep_minutes=60)).json()
    assert body["sleep_minutes"] == 443 and body["steps"] == 9000


def test_a_day_without_a_night_keeps_what_was_stored(api: TestClient) -> None:
    api.post("/api/v1/health/daily", json=sleep_payload())
    again = api.post(
        "/api/v1/health/daily",
        json=sleep_payload(
            day="2026-10-09",
            sleep_starts="",
            sleep_ends="",
            sleep_durations="",
            sleep_values="",
            steps=5,
        ),
    ).json()
    assert again["sleep_minutes"] == 443 and again["steps"] == 5


def test_a_bad_sleep_column_is_refused_with_the_line(api: TestClient) -> None:
    response = api.post("/api/v1/health/daily", json=sleep_payload(sleep_values="Deep\nCore"))
    assert response.status_code == 422
    assert "differ in length" in response.text


def test_some_but_not_all_sleep_columns_are_refused(api: TestClient) -> None:
    response = api.post(
        "/api/v1/health/daily", json={"day": "2026-10-09", "sleep_starts": "8 Oct 2026 at 23:00"}
    )
    assert response.status_code == 422 and "all four" in response.text
