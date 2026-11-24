"""ASGI entry point: `uv run fastapi dev orbit/main.py` / `uvicorn orbit.main:app`."""

from orbit.app import create_app

app = create_app()
