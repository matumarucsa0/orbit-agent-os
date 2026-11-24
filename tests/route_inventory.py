"""Every (method, path) a FastAPI app documents in its OpenAPI schema.

FastAPI keeps included routers as lazy objects with relative paths, so the schema is the stable
way to list routes. Routes with `include_in_schema=False` are not listed; the web pages and the
action links use that, and are covered by their own tests.
"""

from __future__ import annotations

from fastapi import FastAPI

METHODS = {"get", "post", "put", "patch", "delete"}


def all_routes(app: FastAPI) -> list[tuple[str, str]]:
    return sorted(
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
        if method in METHODS
    )
