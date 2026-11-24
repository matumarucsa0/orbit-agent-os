"""Service-layer errors; mapped to HTTP responses by the API and web layers."""

from __future__ import annotations


class NotFoundError(LookupError):
    pass


class InvalidInputError(ValueError):
    pass


class HandleConflictError(Exception):
    """The handle already belongs to another person."""

    def __init__(self, owner_id: int, owner_name: str) -> None:
        super().__init__(f"This handle already belongs to {owner_name}.")
        self.owner_id = owner_id
        self.owner_name = owner_name
