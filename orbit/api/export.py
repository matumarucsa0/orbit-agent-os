from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from orbit.deps import DbSession
from orbit.services import export

router = APIRouter(tags=["export"])


@router.get("/export")
def export_all(db: DbSession) -> dict[str, Any]:
    """Everything in the database (see services/export.py)."""
    return export.export_data(db)
