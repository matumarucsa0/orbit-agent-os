"""The schedule page and API (SPEC §2.21)."""

from __future__ import annotations

from datetime import timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient

from orbit.jobs.scheduler import JobContext
from tests.test_review import FakeReviewer


def values(api: TestClient) -> dict[str, object]:
    return {row["key"]: row for row in api.get("/api/v1/settings/schedule").json()}


# --- API ---


def test_api_requires_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/settings/schedule").status_code == 401
    assert client.patch("/api/v1/settings/schedule", json={"values": {}}).status_code == 401
    assert client.get("/api/v1/jobs").status_code == 401
    assert client.post("/api/v1/jobs/flush_notifications/run").status_code == 401


def test_api_lists_the_settings_with_their_env_defaults(api: TestClient) -> None:
    rows = values(api)
    assert rows["caldav_poll_minutes"]["value"] == 10
    assert rows["quiet_hours"]["value"] == "22:00-08:00"
    assert rows["school_exam_days"]["value"] == [7, 1]
    assert rows["digest_weekday"]["value"] == 6
    assert not any(row["overridden"] for row in rows.values())
    assert "orbit_api_token" not in rows


def test_api_patch_applies_at_once_and_reset_goes_back(api: TestClient, app: FastAPI) -> None:
    response = api.patch(
        "/api/v1/settings/schedule",
        json={
            "values": {
                "digest_time": "17:30",
                "school_exam_days": "10, 3",
                "quiet_hours": "23:00-07:00",
            }
        },
    )
    assert response.status_code == 200
    rows = {r["key"]: r for r in response.json()}
    assert rows["digest_time"]["value"] == "17:30" and rows["digest_time"]["overridden"]
    assert rows["digest_time"]["default"] == "18:00"
    assert rows["school_exam_days"]["value"] == [10, 3]
    # The effective settings the jobs read changed without a restart.
    effective = app.state.jobs.settings()
    assert effective.school_exam_days == [10, 3]
    assert effective.digest_time.hour == 17
    assert app.state.settings.digest_time.hour == 18  # the env copy stays

    reset = api.delete("/api/v1/settings/schedule/digest_time")
    assert reset.status_code == 200
    assert values(api)["digest_time"]["overridden"] is False
    assert app.state.jobs.settings().digest_time.hour == 18


def test_api_patch_rejects_bad_values_and_changes_nothing(api: TestClient) -> None:
    for payload in (
        {"sis_poll_minutes": "5"},
        {"reminder_time": "25:00"},
        {"weekly_review_weekday": "1"},
        {"caldav_poll_minutes": "3", "quiet_hours": "late"},
    ):
        assert api.patch("/api/v1/settings/schedule", json={"values": payload}).status_code == 422
    assert api.patch("/api/v1/settings/schedule", json={"values": {"nope": 1}}).status_code == 404
    assert api.delete("/api/v1/settings/schedule/nope").status_code == 404
    assert not any(row["overridden"] for row in values(api).values())


def test_api_patch_moves_a_running_job(api: TestClient, app: FastAPI) -> None:
    from orbit.jobs.scheduler import start_scheduler

    jobs: JobContext = app.state.jobs
    sched = start_scheduler(jobs.sessions, None, app.state.settings, context=jobs)
    try:
        assert sched.get_job("prune_contact_backups").trigger.interval == timedelta(hours=24)
        api.patch("/api/v1/settings/schedule", json={"values": {"backup_prune_hours": 4}})
        assert sched.get_job("prune_contact_backups").trigger.interval == timedelta(hours=4)
    finally:
        sched.shutdown(wait=False)


def test_api_jobs_and_run_now(api: TestClient) -> None:
    jobs = {j["id"]: j for j in api.get("/api/v1/jobs").json()}
    assert jobs["reminder_pushes"]["every"] == "every 60 s"
    assert jobs["reminder_pushes"]["last_finished_at"] is None
    assert jobs["sync_calendar"]["unavailable"]

    run = api.post("/api/v1/jobs/reminder_pushes/run")
    assert run.status_code == 200
    assert run.json() == {
        "job_id": "reminder_pushes",
        "ran": True,
        "ok": True,
        "message": "0 recorded",
    }
    after = {j["id"]: j for j in api.get("/api/v1/jobs").json()}["reminder_pushes"]
    assert after["last_ok"] is True and after["last_manual"] is True

    refused = api.post("/api/v1/jobs/sync_calendar/run").json()
    assert (refused["ran"], refused["ok"]) == (False, False) and "CALDAV" in refused["message"]
    assert api.post("/api/v1/jobs/flush_usage/run").status_code == 404  # no Run now
    assert api.post("/api/v1/jobs/nope/run").status_code == 404


def test_api_run_now_makes_the_review(api: TestClient, app: FastAPI) -> None:
    app.state.jobs.reviewer = FakeReviewer()
    first = api.post("/api/v1/jobs/weekly_review/run").json()
    assert first["ok"] and first["message"].startswith("review ")
    assert api.post("/api/v1/jobs/weekly_review/run").json()["message"] == "nothing due"


def test_a_switch_edited_in_the_app_stops_the_job(api: TestClient) -> None:
    api.patch("/api/v1/settings/schedule", json={"values": {"school_pushes_enabled": False}})
    result = api.post("/api/v1/jobs/school_pushes/run").json()
    assert (result["ran"], result["message"]) == (False, "Turned off in the settings.")


# --- Web ---


def test_page_needs_login(client: TestClient) -> None:
    response = client.get("/settings/schedule", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    run = client.post("/settings/schedule/jobs/reminder_pushes/run", follow_redirects=False)
    assert run.status_code == 303 and run.headers["location"].startswith("/login")
    for path in ("/settings/schedule", "/settings/schedule/quiet_hours/reset"):
        assert client.post(path, follow_redirects=False).status_code == 303


def test_page_shows_groups_jobs_and_outside_rows(web: TestClient) -> None:
    page = web.get("/settings/schedule")
    assert page.status_code == 200
    for text in (
        "Calendar",
        "Reminders",
        "Weekly review",
        "School",
        "Notifications",
        "Housekeeping",
        'name="caldav_poll_minutes"',
        'name="quiet_hours"',
        'name="digest_weekday"',
        "Run now",
        "Database backup",
        "orbit-review.timer",
    ):
        assert text in page.text
    assert 'name="orbit_api_token"' not in page.text
    assert 'href="/settings/schedule"' in web.get("/").text  # in the nav


def test_saving_a_group_applies_and_shows_what_was_edited(web: TestClient, app: FastAPI) -> None:
    page = web.post(
        "/settings/schedule",
        data={
            "group": "Reminders",
            "reminder_time": "10:00",
            "reminder_close_days_ahead": "5",
            "item_reminder_days": "21",
            "digest_weekday": "4",
            "digest_time": "18:00",
        },
        follow_redirects=True,
    )
    assert "Saved 2 change(s)." in page.text
    assert "Edited. From <code>.env</code>: Sunday" in page.text
    effective = app.state.jobs.settings()
    assert (effective.reminder_time.hour, effective.digest_weekday) == (10, 4)
    assert "Recent changes" in page.text and "Digest day" in page.text
    # Saving again without a change says so.
    again = web.post(
        "/settings/schedule",
        data={"group": "Reminders", "reminder_time": "10:00"},
        follow_redirects=True,
    )
    assert "Nothing changed." in again.text


def test_a_checkbox_saves_on_and_off(web: TestClient, app: FastAPI) -> None:
    off = {"group": "Weekly review", "weekly_review_enabled": ["off"]}
    web.post("/settings/schedule", data=off)
    assert app.state.jobs.settings().weekly_review_enabled is False
    on = {"group": "Weekly review", "weekly_review_enabled": ["off", "on"]}
    web.post("/settings/schedule", data=on)
    assert app.state.jobs.settings().weekly_review_enabled is True


def test_a_bad_value_shows_the_error_and_saves_nothing(web: TestClient, app: FastAPI) -> None:
    page = web.post(
        "/settings/schedule",
        data={"group": "School", "sis_poll_minutes": "5", "school_evening_time": "21:00"},
        follow_redirects=True,
    )
    assert "between 15 and 1440" in page.text
    assert app.state.jobs.settings().school_evening_time.hour == 20
    assert (
        "Unknown group."
        in web.post("/settings/schedule", data={"group": "x"}, follow_redirects=True).text
    )


def test_reset_puts_the_env_value_back(web: TestClient, app: FastAPI) -> None:
    web.post("/settings/schedule", data={"group": "Notifications", "quiet_hours": "23:00-07:00"})
    assert app.state.jobs.settings().quiet_hours[0].hour == 23
    page = web.post("/settings/schedule/quiet_hours/reset", follow_redirects=True)
    assert "back to the value from .env" in page.text
    assert app.state.jobs.settings().quiet_hours[0].hour == 22
    assert (
        "Unknown setting" in web.post("/settings/schedule/nope/reset", follow_redirects=True).text
    )


def test_run_now_from_the_page_shows_the_result_and_the_last_run(web: TestClient) -> None:
    page = web.post("/settings/schedule/jobs/reminder_pushes/run", follow_redirects=True)
    assert "Reminders and digest: 0 recorded" in page.text
    assert "(by hand)" in page.text
    refused = web.post("/settings/schedule/jobs/sync_calendar/run", follow_redirects=True)
    assert "CALDAV" in refused.text
    assert (
        "no Run now"
        in web.post("/settings/schedule/jobs/flush_usage/run", follow_redirects=True).text
    )
    assert "Unknown job" in web.post("/settings/schedule/jobs/nope/run", follow_redirects=True).text
