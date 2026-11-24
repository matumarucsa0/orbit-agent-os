"""Single-password login for the web UI, backed by a signed session cookie."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from orbit.deps import SettingsDep
from orbit.services.passwords import verify_password
from orbit.web.templating import templates

router = APIRouter()

SESSION_KEY = "auth"


def is_logged_in(request: Request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def safe_next(target: str | None) -> str:
    """Only allow local absolute paths as post-login redirect targets (no open redirects)."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


def require_login(request: Request) -> None:
    if not is_logged_in(request):
        target = request.url.path
        if request.url.query:
            target += "?" + request.url.query
        location = f"/login?next={quote(target, safe='')}"
        if request.headers.get("HX-Request"):
            # htmx would swap a redirect's body into the page; make it navigate instead.
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": location}
            )
        raise HTTPException(status_code=status.HTTP_303_SEE_OTHER, headers={"Location": location})


@router.get("/login")
def login_form(request: Request, next: str = "/") -> Response:
    if is_logged_in(request):
        return RedirectResponse(safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request, "login.html", {"next": safe_next(next)})


@router.post("/login")
def login(
    request: Request,
    settings: SettingsDep,
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
) -> Response:
    if verify_password(settings.orbit_web_password_hash.get_secret_value(), password):
        request.session.clear()
        request.session[SESSION_KEY] = True
        return RedirectResponse(safe_next(next), status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": safe_next(next), "error": "Wrong password."},
        status_code=status.HTTP_401_UNAUTHORIZED,
    )


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
