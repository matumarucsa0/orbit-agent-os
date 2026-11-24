"""Edit a vCard in place: change only the properties asked for, keep every other line as it is.

The card is never regenerated. Lines are grouped into logical properties (folded continuation
lines stay with their property); untouched properties keep their exact text, including `X-*`
fields, PHOTO and other apps' data. Written for iCloud's vCard 3.0.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from orbit.domain.enums import HandleType
from orbit.domain.normalize import normalize_handle
from orbit.integrations.vcard import _SCHEMES, _parse_line, _Property, _split_unescaped

FOLD_AT = 75
_OMIT_YEAR = "1604"  # Apple's placeholder year for a birthday without a year


@dataclass(frozen=True)
class SetName:
    display: str
    first: str | None = None
    last: str | None = None


@dataclass(frozen=True)
class SetBirthday:
    month: int | None  # None removes the birthday
    day: int | None = None
    year: int | None = None


@dataclass(frozen=True)
class AddHandle:
    handle_type: HandleType
    value: str


@dataclass(frozen=True)
class RemoveHandle:
    handle_type: HandleType
    value_norm: str


Op = SetName | SetBirthday | AddHandle | RemoveHandle


@dataclass
class _Line:
    raw: str  # the physical line(s), line endings included
    prop: _Property | None

    @property
    def name(self) -> str:
        return self.prop.name if self.prop else ""


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC folding at 75 characters (good enough for ASCII-heavy lines; never splits a char)."""
    if len(line) <= FOLD_AT:
        return line
    parts = [line[:FOLD_AT]]
    rest = line[FOLD_AT:]
    while rest:
        parts.append(" " + rest[: FOLD_AT - 1])
        rest = rest[FOLD_AT - 1 :]
    return "\n".join(parts)


def _split_lines(text: str) -> list[_Line]:
    lines: list[_Line] = []
    # Only CR/LF end a line (str.splitlines would also split on U+2028 and friends).
    for physical in re.findall(r"[^\r\n]*(?:\r\n|\n|\r)|[^\r\n]+", text):
        if physical[:1] in (" ", "\t") and lines:
            lines[-1].raw += physical
        else:
            lines.append(_Line(physical, None))
    for line in lines:
        logical = line.raw.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
        unfolded = re.sub(r"\n[ \t]", "", logical)
        line.prop = _parse_line(unfolded)
    return lines


def _eol(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _value(prop: _Property) -> str:
    value = prop.value.strip()
    lowered = value.lower()
    for scheme in _SCHEMES:
        if lowered.startswith(scheme):
            return value[len(scheme) :]
    return value


def _handle_matches(prop: _Property, handle_type: HandleType, value_norm: str, region: str) -> bool:
    if handle_type is HandleType.PHONE:
        return (
            prop.name == "TEL" and normalize_handle(handle_type, _value(prop), region) == value_norm
        )
    if handle_type is HandleType.EMAIL:
        return prop.name == "EMAIL" and _value(prop).casefold() == value_norm
    if prop.name == "X-SOCIALPROFILE":
        service = (prop.params.get("type") or [""])[0].lower()
        user = (prop.params.get("x-user") or [""])[0] or _value(prop)
        return service == handle_type.value and (
            normalize_handle(handle_type, user, region) == value_norm
        )
    if prop.name == "IMPP":
        service = (prop.params.get("x-service-type") or prop.params.get("type") or [""])[0]
        return service.lower() == handle_type.value and (
            normalize_handle(handle_type, _value(prop), region) == value_norm
        )
    return False


def _new_handle_line(op: AddHandle) -> str:
    value = op.value.strip()
    if op.handle_type is HandleType.PHONE:
        return f"TEL;type=CELL;type=VOICE:{_escape(value)}"
    if op.handle_type is HandleType.EMAIL:
        return f"EMAIL;type=INTERNET;type=HOME:{_escape(value)}"
    user = value.rstrip("/").rsplit("/", 1)[-1].lstrip("@")
    kind = op.handle_type.value
    urls = {
        "instagram": f"https://instagram.com/{user}",
        "telegram": f"https://t.me/{user}",
        "linkedin": value if value.startswith("http") else f"https://www.linkedin.com/in/{user}",
    }
    return f"X-SOCIALPROFILE;type={kind};x-user={user}:{urls.get(kind, value)}"


def _birthday_line(op: SetBirthday) -> str:
    assert op.month is not None and op.day is not None
    if op.year is not None:
        return f"BDAY:{op.year:04d}-{op.month:02d}-{op.day:02d}"
    return f"BDAY;X-APPLE-OMIT-YEAR={_OMIT_YEAR}:{_OMIT_YEAR}-{op.month:02d}-{op.day:02d}"


def _name_lines(op: SetName, lines: list[_Line]) -> dict[str, str]:
    out = {"FN": f"FN:{_escape(op.display)}"}
    existing = next((ln.prop for ln in lines if ln.name == "N" and ln.prop), None)
    if op.first is not None or op.last is not None:
        parts = _split_unescaped(existing.value, ";") if existing is not None else []
        parts += [""] * (5 - len(parts))
        if op.last is not None:
            parts[0] = _escape(op.last)
        if op.first is not None:
            parts[1] = _escape(op.first)
        out["N"] = "N:" + ";".join(parts[:5])
    return out


def _replace_or_add(lines: list[_Line], name: str, new: str | None, eol: str) -> None:
    indexes = [i for i, ln in enumerate(lines) if ln.name == name]
    if new is None:
        for i in reversed(indexes):
            del lines[i]
        return
    text = _fold(new).replace("\n", eol) + eol
    if indexes:
        lines[indexes[0]] = _Line(text, _parse_line(new))
        for i in reversed(indexes[1:]):
            del lines[i]
    else:
        _insert(lines, _Line(text, _parse_line(new)))


def _insert(lines: list[_Line], line: _Line) -> None:
    end = next((i for i, ln in enumerate(lines) if ln.name == "END"), len(lines))
    lines.insert(end, line)


def _drop_orphan_label(lines: list[_Line], group: str | None) -> None:
    if not group:
        return
    members = [ln for ln in lines if ln.prop and ln.prop.group == group]
    if members and all(ln.name == "X-ABLABEL" for ln in members):
        for ln in members:
            lines.remove(ln)


def patch_vcard(text: str, ops: list[Op], region: str) -> str:
    """The card text with `ops` applied; identical to `text` when nothing needs to change."""
    lines = _split_lines(text)
    eol = _eol(text)
    if lines and not lines[-1].raw.endswith(("\n", "\r")):
        lines[-1].raw += eol
    for op in ops:
        if isinstance(op, SetName):
            for name, name_line in _name_lines(op, lines).items():
                _replace_or_add(lines, name, name_line, eol)
        elif isinstance(op, SetBirthday):
            new = None if op.month is None else _birthday_line(op)
            _replace_or_add(lines, "BDAY", new, eol)
        elif isinstance(op, AddHandle):
            if not any(
                ln.prop
                and _handle_matches(
                    ln.prop,
                    op.handle_type,
                    normalize_handle(op.handle_type, op.value, region),
                    region,
                )
                for ln in lines
            ):
                new = _new_handle_line(op)
                _insert(lines, _Line(_fold(new).replace("\n", eol) + eol, _parse_line(new)))
        else:
            for ln in list(lines):
                if ln.prop and _handle_matches(ln.prop, op.handle_type, op.value_norm, region):
                    lines.remove(ln)
                    _drop_orphan_label(lines, ln.prop.group)
    return "".join(ln.raw for ln in lines)
