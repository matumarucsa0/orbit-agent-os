import copy
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

POSTS: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "liftoff" / "post_getMyPosts.json").read_text()
)


class FakeSource:
    def workouts(self) -> list[dict[str, Any]]:
        return copy.deepcopy(POSTS)


def test_without_liftoff_the_page_says_why(web: TestClient) -> None:
    page = web.get("/fitness")
    assert page.status_code == 200
    assert "Liftoff is not set up" in page.text
    assert "No workouts yet" in page.text
    assert 'action="/fitness/sync"' not in page.text


def test_read_now_pulls_the_workouts_and_the_pages_show_them(web: TestClient) -> None:
    web.app.state.jobs.workouts = FakeSource()  # type: ignore[attr-defined]
    done = web.post("/fitness/sync", follow_redirects=True)
    assert "4 new, 0 changed, 0 unchanged" in done.text
    page = web.get("/fitness")
    assert "4 in all" in page.text
    first = page.text.split('href="/fitness/')[1].split('"')[0]
    detail = web.get(f"/fitness/{first}")
    assert detail.status_code == 200
    assert "kg" in detail.text or "reps" in detail.text


def test_a_failing_liftoff_is_reported_not_a_crash(web: TestClient) -> None:
    class Broken:
        def workouts(self) -> list[dict[str, Any]]:
            raise RuntimeError("down")

    web.app.state.jobs.workouts = Broken()  # type: ignore[attr-defined]
    page = web.post("/fitness/sync", follow_redirects=True)
    assert page.status_code == 200
    assert "down" in page.text


def test_an_unknown_workout_is_not_found(web: TestClient) -> None:
    assert web.get("/fitness/999").status_code == 404


def test_the_pages_need_a_login(client: TestClient) -> None:
    assert client.get("/fitness", follow_redirects=False).status_code in (303, 401)
