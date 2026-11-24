from __future__ import annotations

import re

from fastapi.testclient import TestClient


def add(web: TestClient, name: str, **extra: str) -> int:
    response = web.post("/people", data={"name": name, **extra}, follow_redirects=False)
    assert response.status_code == 303, response.text
    match = re.fullmatch(r"/people/(\d+)", response.headers["location"])
    assert match, response.headers["location"]
    return int(match.group(1))


def test_people_pages_require_login(client: TestClient) -> None:
    for path in ("/people", "/people/1", "/people/duplicates", "/people/1/merge"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/login?next=")


def test_htmx_request_gets_hx_redirect_when_logged_out(client: TestClient) -> None:
    response = client.get("/people?q=x", headers={"HX-Request": "true"}, follow_redirects=False)
    assert response.status_code == 401
    assert response.headers["hx-redirect"] == "/login?next=%2Fpeople%3Fq%3Dx"


def test_add_name_only_person(web: TestClient) -> None:
    person_id = add(web, "Terka")
    page = web.get(f"/people/{person_id}")
    assert page.status_code == 200
    assert "Terka" in page.text
    assert "Added Terka." in page.text  # flash
    assert "Added Terka." not in web.get(f"/people/{person_id}").text  # shown once


def test_add_instagram_only_person(web: TestClient) -> None:
    person_id = add(web, "@someone")
    page = web.get(f"/people/{person_id}").text
    assert "<h1>someone" in page
    assert "instagram" in page and "@someone" in page
    # Adding the same IG handle again goes to the existing person instead.
    again = web.post("/people", data={"name": "@someone"}, follow_redirects=False)
    assert again.headers["location"] == f"/people/{person_id}"


def test_empty_name_rejected(web: TestClient) -> None:
    response = web.post("/people", data={"name": "   "})
    assert "A person needs a name." in response.text


def test_search_pato_with_and_without_diacritics(web: TestClient) -> None:
    person_id = add(web, "Patrik Veselý")
    web.post(f"/people/{person_id}/aliases", data={"value": "Paťo"})
    add(web, "Terka")
    web.get("/people")  # consume the "Added …" flash messages
    for query in ("pato", "paťo", "PATO"):
        page = web.get("/people", params={"q": query}).text
        assert "Patrik Veselý" in page and "Terka" not in page
    # The htmx live search returns only the results fragment.
    fragment = web.get(
        "/people", params={"q": "pato"}, headers={"HX-Request": "true", "HX-Target": "results"}
    ).text
    assert "Patrik Veselý" in fragment and "<html" not in fragment


def test_detail_edits(web: TestClient) -> None:
    pid = add(web, "Patrik Veselý")
    web.post(
        f"/people/{pid}/edit",
        data={
            "display_name": "Patrik Veselý",
            "first_name": "Patrik",
            "last_name": "Veselý",
            "birthday_day": "17",
            "birthday_month": "3",
            "birthday_year": "",
            "nameday_country": "SK",
            "country": "sk",
            "city": "Košice",
            "how_met": "Uni",
            "photo_url": "",
        },
    )
    web.post(f"/people/{pid}/handles", data={"type": "phone", "value": "0905 123 456"})
    web.post(f"/people/{pid}/tags", data={"name": "gym"})
    web.post(f"/people/{pid}/notes", data={"text": "Loves climbing"})
    web.post(
        f"/people/{pid}/interactions",
        data={"occurred_on": "2026-09-20", "kind": "call", "summary": "Catch-up"},
    )
    page = web.get(f"/people/{pid}").text
    for expected in ("Košice", "17 Mar", "0905 123 456", "#gym", "Loves climbing", "Catch-up"):
        assert expected in page, expected
    assert "Last talked: 20 Sep 2026 (call)" in page
    assert web.get("/people", params={"q": "0905"}).text.count("Patrik Veselý") >= 1


def test_invalid_edit_shows_error(web: TestClient) -> None:
    pid = add(web, "X")
    response = web.post(
        f"/people/{pid}/edit",
        data={"display_name": "X", "birthday_day": "31", "birthday_month": "2"},
    )
    assert "That birthday date doesn&#39;t exist." in response.text
    bad_type = web.post(
        f"/people/{pid}/edit",
        data={"display_name": "X", "birthday_day": "abc"},
        headers={"referer": f"http://testserver/people/{pid}"},
    )
    assert bad_type.url.path == f"/people/{pid}"
    assert "Please check: birthday_day" in bad_type.text


def test_handle_conflict_offers_merge(web: TestClient) -> None:
    a = add(web, "Pato")
    b = add(web, "Patrik")
    web.post(f"/people/{a}/handles", data={"type": "email", "value": "pato@example.com"})
    page = web.post(f"/people/{b}/handles", data={"type": "email", "value": "PATO@example.com"})
    assert "already belongs to Pato" in page.text
    assert f"/people/{b}/merge?with={a}" in page.text


def test_merge_flow(web: TestClient) -> None:
    a = add(web, "Terézia Nováková")
    b = add(web, "terezia novakova")
    web.post(f"/people/{b}/notes", data={"text": "note on the duplicate"})

    duplicates_page = web.get("/people/duplicates").text
    assert "Same name" in duplicates_page
    assert "possible duplicate" in web.get("/people").text

    preview = web.get(f"/people/{a}/merge", params={"with": b}).text
    assert "Keep" in preview and "1 notes" in preview

    done = web.post("/people/merge", data={"keep_id": a, "merge_id": b})
    assert done.url.path == f"/people/{a}"
    assert "Merged terezia novakova into Terézia Nováková." in done.text
    assert "note on the duplicate" in done.text
    assert web.get(f"/people/{b}").status_code == 404
    assert "No likely duplicates" in web.get("/people/duplicates").text


def test_merge_picker_search(web: TestClient) -> None:
    a = add(web, "Pato")
    add(web, "Patrik Veselý")
    page = web.get(f"/people/{a}/merge", params={"q": "patrik"}).text
    assert "Patrik Veselý" in page


def test_not_a_duplicate(web: TestClient) -> None:
    a = add(web, "Martin Horváth")
    b = add(web, "Martin Horváth")
    page = web.post("/people/duplicates/dismiss", data={"a_id": a, "b_id": b})
    assert "Marked as not a duplicate." in page.text
    assert "No likely duplicates" in page.text


def test_archive_and_delete(web: TestClient) -> None:
    pid = add(web, "Old Friend")
    web.post(f"/people/{pid}/archive")
    assert "Old Friend" not in web.get("/people").text
    web.post(f"/people/{pid}/archive")
    assert "Old Friend" in web.get("/people").text
    page = web.post(f"/people/{pid}/delete")
    assert "Deleted Old Friend." in page.text
    assert web.get(f"/people/{pid}").status_code == 404


def test_not_found_page(web: TestClient) -> None:
    response = web.get("/people/999")
    assert response.status_code == 404
    assert "Person 999 not found." in response.text
