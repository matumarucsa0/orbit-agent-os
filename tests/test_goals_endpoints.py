from __future__ import annotations

from fastapi.testclient import TestClient

from tests.test_api_ledger import person


def add_goal(api: TestClient, title: str, **body: object) -> dict[str, object]:
    response = api.post("/api/v1/goals", json={"title": title, **body})
    assert response.status_code == 201, response.text
    return response.json()


def add_promise(api: TestClient, person_id: int, text: str = "send the plan") -> int:
    response = api.post(
        "/api/v1/promises", json={"person_id": person_id, "direction": "i_promised", "text": text}
    )
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# --- API ---


def test_api_requires_the_token(client: TestClient) -> None:
    assert client.get("/api/v1/goals").status_code == 401
    assert client.post("/api/v1/goals", json={"title": "x"}).status_code == 401


def test_api_create_list_get_patch(api: TestClient) -> None:
    first = add_goal(api, "Run a half marathon", why="energy", horizon="year", target="2:00")
    second = add_goal(api, "Save 5000")
    assert (first["rank"], second["rank"]) == (1, 2)
    assert first["status"] == "active"
    listed = api.get("/api/v1/goals").json()
    assert [g["title"] for g in listed] == ["Run a half marathon", "Save 5000"]
    got = api.get(f"/api/v1/goals/{first['id']}").json()
    assert (got["why"], got["horizon"], got["target"]) == ("energy", "year", "2:00")
    patched = api.patch(
        f"/api/v1/goals/{first['id']}", json={"target": "1:55", "target_date": "2027-04-01"}
    ).json()
    assert (patched["target"], patched["target_date"], patched["why"]) == (
        "1:55",
        "2027-04-01",
        "energy",
    )
    assert api.get("/api/v1/goals/999").status_code == 404


def test_api_validation(api: TestClient) -> None:
    assert api.post("/api/v1/goals", json={"title": "  "}).status_code == 422
    assert api.post("/api/v1/goals", json={"title": "x", "horizon": "decade"}).status_code == 422
    add_goal(api, "Run")
    assert api.post("/api/v1/goals", json={"title": "run"}).status_code == 422  # duplicate
    first = api.get("/api/v1/goals").json()[0]
    assert api.patch(f"/api/v1/goals/{first['id']}", json={"title": None}).status_code == 422
    assert api.patch(f"/api/v1/goals/{first['id']}", json={"status": None}).status_code == 422


def test_api_status_and_filters(api: TestClient) -> None:
    a, b = add_goal(api, "A"), add_goal(api, "B")
    paused = api.patch(f"/api/v1/goals/{a['id']}", json={"status": "paused"}).json()
    assert (paused["status"], paused["rank"]) == ("paused", None)
    assert [(g["title"], g["rank"]) for g in api.get("/api/v1/goals").json()] == [("B", 1)]
    assert [g["title"] for g in api.get("/api/v1/goals?status=paused").json()] == ["A"]
    assert [g["title"] for g in api.get("/api/v1/goals?status=all").json()] == ["B", "A"]
    done = api.patch(f"/api/v1/goals/{b['id']}", json={"status": "done"}).json()
    assert done["closed_at"] is not None


def test_api_rank(api: TestClient) -> None:
    a, b, c = add_goal(api, "A"), add_goal(api, "B"), add_goal(api, "C")
    ranked = api.post("/api/v1/goals/rank", json={"ids": [c["id"], a["id"], b["id"]]}).json()
    assert [(g["title"], g["rank"]) for g in ranked] == [("C", 1), ("A", 2), ("B", 3)]
    assert api.post("/api/v1/goals/rank", json={"ids": [a["id"]]}).status_code == 422
    assert api.post("/api/v1/goals/rank", json={"ids": [a["id"]] * 3}).status_code == 422


def test_api_review_people_and_promises(api: TestClient) -> None:
    goal = add_goal(api, "Get fit")
    pato = person(api, "Patrik Veselý")
    promise = add_promise(api, pato)
    gid = goal["id"]
    reviewed = api.post(f"/api/v1/goals/{gid}/review").json()
    assert reviewed["reviewed_at"] is not None
    linked = api.post(f"/api/v1/goals/{gid}/people", json={"person_id": pato}).json()
    assert [p["display_name"] for p in linked["people"]] == ["Patrik Veselý"]
    assert api.post(f"/api/v1/goals/{gid}/people", json={"person_id": 999}).status_code == 404
    attached = api.post(f"/api/v1/goals/{gid}/promises", json={"promise_id": promise}).json()
    assert [p["text"] for p in attached["promises"]] == ["send the plan"]
    assert api.get(f"/api/v1/promises/{promise}").json()["goal_id"] == gid
    removed = api.delete(f"/api/v1/goals/{gid}/promises/{promise}").json()
    assert removed["promises"] == []
    assert api.delete(f"/api/v1/goals/{gid}/promises/{promise}").status_code == 404
    assert api.delete(f"/api/v1/goals/{gid}/people/{pato}").json()["people"] == []


def test_api_promise_goal_field(api: TestClient) -> None:
    goal = add_goal(api, "Get fit")
    pato = person(api, "Patrik Veselý")
    created = api.post(
        "/api/v1/promises",
        json={"person_id": pato, "direction": "i_promised", "text": "x", "goal_id": goal["id"]},
    )
    assert created.status_code == 201
    assert created.json()["goal_id"] == goal["id"]
    pid = created.json()["id"]
    assert api.patch(f"/api/v1/promises/{pid}", json={"goal_id": None}).json()["goal_id"] is None
    assert api.patch(f"/api/v1/promises/{pid}", json={"goal_id": 999}).status_code == 404


def test_api_delete_keeps_the_promise(api: TestClient) -> None:
    a, b = add_goal(api, "A"), add_goal(api, "B")
    pato = person(api, "Patrik Veselý")
    promise = add_promise(api, pato)
    api.post(f"/api/v1/goals/{a['id']}/promises", json={"promise_id": promise})
    assert api.delete(f"/api/v1/goals/{a['id']}").status_code == 204
    assert api.get(f"/api/v1/promises/{promise}").json()["goal_id"] is None
    assert [(g["title"], g["rank"]) for g in api.get("/api/v1/goals").json()] == [("B", 1)]
    assert api.get(f"/api/v1/goals/{b['id']}").status_code == 200


def test_api_goals_are_in_the_export_and_the_context(api: TestClient) -> None:
    add_goal(api, "Get fit", why="my back hurts")
    exported = api.get("/api/v1/export").json()
    assert [g["title"] for g in exported["tables"]["goal"]] == ["Get fit"]
    context = api.get("/api/v1/context").json()
    assert [g["title"] for g in context["goals"]["goals"]] == ["Get fit"]
    assert context["goals"]["goals"][0]["why"] == "my back hurts"


# --- Web ---


def test_web_requires_login(client: TestClient) -> None:
    for path in ("/goals", "/goals/review", "/goals/1"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/login")


def test_web_add_list_and_move(web: TestClient, api: TestClient) -> None:
    for title in ("Marathon plan", "Save 5000"):
        response = web.post(
            "/goals", data={"title": title, "horizon": "year"}, follow_redirects=True
        )
        assert response.status_code == 200
    page = web.get("/goals").text
    assert page.index("Marathon plan") < page.index("Save 5000")
    ids = [g["id"] for g in web.get("/api/v1/goals").json()]
    web.post(f"/goals/{ids[1]}/move", data={"direction": "top"})
    page = web.get("/goals").text
    assert page.index("Save 5000") < page.index("Marathon plan")


def test_web_add_errors_are_flashed(web: TestClient, api: TestClient) -> None:
    web.post("/goals", data={"title": "Run"})
    response = web.post("/goals", data={"title": "run"}, follow_redirects=True)
    assert "already have an active goal" in response.text
    response = web.post("/goals", data={"title": "   "}, follow_redirects=True)
    assert "A goal needs a title." in response.text


def test_web_detail_edit_status_links_and_delete(web: TestClient, api: TestClient) -> None:
    pato = person(web, "Patrik Veselý")
    web.post("/goals", data={"title": "Get fit"})
    gid = web.get("/api/v1/goals").json()[0]["id"]
    promise = add_promise(web, pato)
    page = web.get(f"/goals/{gid}").text
    assert "Get fit" in page
    assert "Patrik Veselý" in page  # in the pickers
    web.post(
        f"/goals/{gid}/edit",
        data={
            "title": "Get fitter",
            "why": "back",
            "horizon": "month",
            "target": "10 km",
            "target_date": "2026-12-01",
        },
    )
    shown = web.get(f"/goals/{gid}").text
    assert "Get fitter" in shown and "10 km" in shown and "back" in shown
    web.post(f"/goals/{gid}/people", data={"person_id": pato})
    web.post(f"/goals/{gid}/promises", data={"promise_id": promise})
    detail = web.get(f"/goals/{gid}").text
    assert "send the plan" in detail
    assert f"/goals/{gid}/people/{pato}/remove" in detail
    assert "Get fitter" in web.get(f"/people/{pato}").text  # the person page lists its goals
    bad = web.post(
        f"/goals/{gid}/edit", data={"title": "x", "target_date": "nope"}, follow_redirects=True
    )
    assert "Not a date: nope" in bad.text
    web.post(f"/goals/{gid}/status", data={"status": "paused"})
    assert "Paused" in web.get(f"/goals/{gid}").text
    web.post(f"/goals/{gid}/promises/{promise}/remove")
    web.post(f"/goals/{gid}/people/{pato}/remove")
    web.post(f"/goals/{gid}/delete")
    assert web.get(f"/goals/{gid}").status_code == 404


def test_web_review_flow(web: TestClient, api: TestClient) -> None:
    for title in ("A", "B", "C"):
        web.post("/goals", data={"title": title})
    goals_ = web.get("/api/v1/goals").json()
    a, b, c = (g["id"] for g in goals_)
    page = web.get("/goals/review").text
    assert "Review your goals" in page and "Still right" in page
    web.post(f"/goals/review/{c}", data={"action": "up"})
    web.post(f"/goals/review/{a}", data={"action": "pause"})
    web.post(f"/goals/review/{b}", data={"action": "keep"})
    order = [(g["title"], g["rank"]) for g in web.get("/api/v1/goals").json()]
    assert order == [("C", 1), ("B", 2)]
    web.post(f"/goals/review/{c}", data={"action": "done"})
    assert [g["title"] for g in web.get("/api/v1/goals").json()] == ["B"]
    bad = web.post(f"/goals/review/{b}", data={"action": "explode"}, follow_redirects=True)
    assert "Unknown action." in bad.text


def test_web_home_shows_goals_and_the_nudge(web: TestClient, api: TestClient) -> None:
    for title in ("A", "B", "C", "D"):
        web.post("/goals", data={"title": title})
    home = web.get("/").text
    assert "🎯 1. A" in home and "🎯 3. C" in home
    assert "🎯 4. D" not in home  # only the top three
    assert "Review your goals" not in home  # reviewed just now


def test_web_promise_forms_take_a_goal(web: TestClient, api: TestClient) -> None:
    pato = person(web, "Patrik Veselý")
    web.post("/goals", data={"title": "Get fit"})
    gid = web.get("/api/v1/goals").json()[0]["id"]
    web.post(
        f"/people/{pato}/promises",
        data={"direction": "i_promised", "text": "send the plan", "goal": str(gid)},
    )
    promise = web.get("/api/v1/promises").json()[0]
    assert promise["goal_id"] == gid
    assert "🎯 Get fit" in web.get(f"/people/{pato}").text
    # An edit without the goal field leaves it; a blank one clears it.
    web.post(f"/promises/{promise['id']}/edit", data={"direction": "i_promised", "text": "x"})
    assert web.get(f"/api/v1/promises/{promise['id']}").json()["goal_id"] == gid
    web.post(
        f"/promises/{promise['id']}/edit",
        data={"direction": "i_promised", "text": "x", "goal": "", "goal_set": "1"},
    )
    assert web.get(f"/api/v1/promises/{promise['id']}").json()["goal_id"] is None


def test_web_nav_has_goals_and_context_shows_them(web: TestClient, api: TestClient) -> None:
    web.post("/goals", data={"title": "Get fit"})
    assert 'href="/goals"' in web.get("/").text
    assert "1 goal" in web.get("/context").text
