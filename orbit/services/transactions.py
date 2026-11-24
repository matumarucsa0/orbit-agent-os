"""Browsing bank rows across all imports: a filter plus a diacritics-insensitive search."""

from __future__ import annotations

from typing import Literal

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session

from orbit.domain.bank import Transaction
from orbit.domain.enums import Classification, TransactionDirection
from orbit.domain.money import parse_amount
from orbit.domain.normalize import normalize_text
from orbit.services.search import like_pattern

Show = Literal["shared", "received", "unclassified", "all"]


def filtered(query: Select[tuple[Transaction]], show: Show) -> Select[tuple[Transaction]]:
    """Narrow to one view: flagged as likely shared, incoming, not classified yet, or all."""
    if show == "shared":
        return query.where(Transaction.classification == Classification.LIKELY_SHARED)
    if show == "received":
        return query.where(Transaction.direction == TransactionDirection.IN)
    if show == "unclassified":
        return query.where(Transaction.classification.is_(None))
    return query


SHOWS: tuple[Show, ...] = ("shared", "received", "unclassified", "all")


def count_by_show(session: Session, import_id: int | None = None) -> dict[Show, int]:
    """How many rows each view has (of one import, or of all of them)."""
    base = select(Transaction)
    if import_id is not None:
        base = base.where(Transaction.import_id == import_id)
    return {
        show: session.scalar(select(func.count()).select_from(filtered(base, show).subquery())) or 0
        for show in SHOWS
    }


def search_transactions(
    session: Session,
    q: str = "",
    *,
    show: Show = "all",
    currency: str = "EUR",
    limit: int = 50,
    offset: int = 0,
) -> list[Transaction]:
    """Newest first. Every word of `q` must match the merchant / counterparty or the payment
    message ("pizza", "vesely" finds "VESELÝ"); or `q` is an amount ("48", "48,00"; with the
    decimal places of `currency`)."""
    query = filtered(select(Transaction), show)
    words = normalize_text(q).split()
    if words:
        text_match: ColumnElement[bool] = and_(
            *(
                or_(
                    Transaction.counterparty_norm.like(like_pattern(w), escape="\\"),
                    Transaction.message_norm.like(like_pattern(w), escape="\\"),
                )
                for w in words
            )
        )
        try:
            query = query.where(
                or_(text_match, Transaction.amount_minor == parse_amount(q, currency))
            )
        except ValueError:
            query = query.where(text_match)
    query = query.order_by(Transaction.booking_date.desc(), Transaction.id.desc())
    return list(session.scalars(query.limit(limit).offset(offset)))
