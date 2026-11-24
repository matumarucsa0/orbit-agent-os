"""Diacritics-insensitive people search across names, aliases, handles, tags and notes."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, case, or_, select, true
from sqlalchemy.orm import Session, selectinload

from orbit.domain.enums import PHONE_HANDLE_TYPES
from orbit.domain.normalize import normalize_text
from orbit.domain.people import Alias, Handle, Note, Person, Tag, person_tag

MIN_PHONE_DIGITS = 3


def like_pattern(token: str) -> str:
    """`%token%` for LIKE with `escape="\\"`, the token's own `%`, `_` and `\\` escaped."""
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _token_matches(token: str) -> ColumnElement[bool]:
    pattern = like_pattern(token)
    alternatives: list[ColumnElement[bool]] = [
        Person.search_name_norm.like(pattern, escape="\\"),
        select(Alias.id)
        .where(Alias.person_id == Person.id, Alias.value_norm.like(pattern, escape="\\"))
        .exists(),
        select(Handle.id)
        .where(Handle.person_id == Person.id, Handle.value_norm.like(pattern, escape="\\"))
        .exists(),
        select(Tag.id)
        .join(person_tag, person_tag.c.tag_id == Tag.id)
        .where(person_tag.c.person_id == Person.id, Tag.name_norm.like(pattern, escape="\\"))
        .exists(),
        select(Note.id)
        .where(Note.person_id == Person.id, Note.text_norm.like(pattern, escape="\\"))
        .exists(),
    ]
    # "0905 123" should find the same number stored with its country code: drop the trunk-prefix
    # zeros for phone matching.
    digits = token.lstrip("+").lstrip("0")
    if digits.isdigit() and len(digits) >= MIN_PHONE_DIGITS:
        alternatives.append(
            select(Handle.id)
            .where(
                Handle.person_id == Person.id,
                Handle.type.in_(PHONE_HANDLE_TYPES),
                Handle.value_norm.like(like_pattern(digits), escape="\\"),
            )
            .exists()
        )
    return or_(*alternatives)


def _tokens(query: str) -> list[str]:
    tokens = []
    for token in normalize_text(query).split():
        stripped = token.lstrip("@")  # "@pato" searches the handle "pato"
        if stripped:
            tokens.append(stripped)
    return tokens


def search_people(
    session: Session,
    query: str = "",
    *,
    tag: str | None = None,
    include_archived: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> list[Person]:
    """Every query word must match somewhere; name-prefix matches rank first."""
    tokens = _tokens(query)
    conditions: list[ColumnElement[bool]] = [_token_matches(t) for t in tokens]
    if not include_archived:
        conditions.append(Person.archived.is_(False))
    if tag:
        conditions.append(
            select(Tag.id)
            .join(person_tag, person_tag.c.tag_id == Tag.id)
            .where(person_tag.c.person_id == Person.id, Tag.name_norm == normalize_text(tag))
            .exists()
        )

    stmt = select(Person).options(selectinload(Person.tags)).where(and_(true(), *conditions))
    if tokens:
        prefix = tokens[0].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rank = case(
            (Person.search_name_norm.like(prefix, escape="\\"), 0),
            (Person.search_name_norm.like(like_pattern(" " + tokens[0]), escape="\\"), 1),
            else_=2,
        )
        stmt = stmt.order_by(rank)
    stmt = stmt.order_by(Person.search_name_norm, Person.id).limit(limit).offset(offset)
    return list(session.scalars(stmt))
