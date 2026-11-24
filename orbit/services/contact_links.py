"""Tappable links for a handle: call, text, email, open the app. Presentation only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote

from orbit.domain.enums import HandleType


@dataclass(frozen=True)
class ContactLink:
    label: str  # short verb for a button: "Call", "Text", "Email"...
    url: str


def _digits(value: str) -> str:
    """Digits and a leading +: "+44 7700 900123" -> "+447700900123"."""
    plus = "+" if value.strip().startswith(("+", "00")) else ""
    digits = re.sub(r"\D", "", value)
    if value.strip().startswith("00"):
        digits = digits[2:]
    return plus + digits


def _username(value: str) -> str:
    """The bare name: "@pato.vesely" or "instagram.com/pato.vesely/" -> "pato.vesely"."""
    value = value.strip().rstrip("/")
    value = value.rsplit("/", 1)[-1]
    return value.lstrip("@")


def links_for(handle_type: HandleType, value: str) -> list[ContactLink]:
    """What a tap on this handle can do; empty when there's nothing sensible to open."""
    value = value.strip()
    if not value:
        return []
    if handle_type is HandleType.PHONE:
        number = _digits(value)
        if len(number.lstrip("+")) < 3:
            return []
        return [ContactLink("Call", f"tel:{number}"), ContactLink("Text", f"sms:{number}")]
    if handle_type is HandleType.WHATSAPP:
        number = _digits(value).lstrip("+")
        return [ContactLink("WhatsApp", f"https://wa.me/{number}")] if len(number) >= 3 else []
    if handle_type is HandleType.EMAIL:
        if "@" not in value:
            return []
        return [ContactLink("Email", f"mailto:{quote(value, safe='@.+-_')}")]
    if handle_type is HandleType.INSTAGRAM:
        name = _username(value)
        return [ContactLink("Instagram", f"https://instagram.com/{quote(name)}")] if name else []
    if handle_type is HandleType.TELEGRAM:
        name = _username(value)
        return [ContactLink("Telegram", f"https://t.me/{quote(name)}")] if name else []
    if handle_type is HandleType.LINKEDIN:
        if value.startswith(("http://", "https://")):
            return [ContactLink("LinkedIn", value)]
        name = _username(value)
        return [ContactLink("LinkedIn", f"https://www.linkedin.com/in/{quote(name)}")]
    return []
