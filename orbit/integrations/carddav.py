"""CardDAV client (iCloud Contacts): discovers the address books and fetches all vCards.

Flow: current-user-principal -> addressbook-home-set -> address books (PROPFIND depth 1)
-> vCard hrefs (PROPFIND depth 1) -> addressbook-multiget in batches.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import httpx

DAV = "DAV:"
CARDDAV = "urn:ietf:params:xml:ns:carddav"
_NS = f'xmlns:D="{DAV}" xmlns:C="{CARDDAV}"'
MULTIGET_BATCH = 200


class CardDavError(Exception):
    pass


class CardChangedError(CardDavError):
    """The card changed on the server since it was read (HTTP 412 on a conditional write)."""


@dataclass(frozen=True)
class RawCard:
    """One vCard as the server holds it: the text plus where it lives and its version."""

    text: str
    href: str | None = None
    etag: str | None = None


class ContactSource(Protocol):
    def fetch_cards(self) -> list[RawCard]: ...

    def get_card(self, href: str) -> RawCard: ...

    def put_card(self, href: str, text: str, etag: str | None) -> str | None:
        """Write a card if it still has version `etag`; returns the new version, if given."""
        ...


def _tag(ns: str, name: str) -> str:
    return f"{{{ns}}}{name}"


class CardDavClient:
    def __init__(self, base_url: str, http: httpx.Client) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.http = http

    @classmethod
    def connect(cls, base_url: str, username: str, password: str) -> CardDavClient:
        http = httpx.Client(auth=(username, password), timeout=30.0, follow_redirects=True)
        return cls(base_url, http)

    def _request(self, method: str, url: str, body: str, depth: str) -> ET.Element:
        try:
            response = self.http.request(
                method,
                url,
                content=body.encode(),
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            )
        except httpx.HTTPError as exc:
            raise CardDavError(f"Could not reach the contacts server: {exc}") from exc
        if response.status_code == 401:
            raise CardDavError(
                "The contacts server rejected the username or app-specific password."
            )
        if response.status_code not in (200, 207):
            raise CardDavError(f"{method} {url} failed with HTTP {response.status_code}.")
        try:
            return ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise CardDavError(f"Invalid XML from the contacts server: {exc}") from exc

    def _propfind(self, url: str, props: str, depth: str) -> ET.Element:
        body = (
            f'<?xml version="1.0" encoding="utf-8"?>'
            f"<D:propfind {_NS}><D:prop>{props}</D:prop></D:propfind>"
        )
        return self._request("PROPFIND", url, body, depth)

    def _single_href(self, url: str, prop_ns: str, prop: str, prefix: str) -> str:
        root = self._propfind(url, f"<{prefix}:{prop}/>", "0")
        href = root.find(f".//{_tag(prop_ns, prop)}/{_tag(DAV, 'href')}")
        if href is None or not (href.text or "").strip():
            raise CardDavError(f"The contacts server did not return {prop}.")
        return urljoin(url, (href.text or "").strip())

    def _addressbooks(self, home_url: str) -> list[str]:
        root = self._propfind(home_url, "<D:resourcetype/>", "1")
        books = []
        for response in root.iter(_tag(DAV, "response")):
            href = response.findtext(_tag(DAV, "href"), default="").strip()
            if href and response.find(f".//{_tag(CARDDAV, 'addressbook')}") is not None:
                books.append(urljoin(home_url, href))
        return books

    def _card_hrefs(self, book_url: str) -> list[str]:
        root = self._propfind(book_url, "<D:getcontenttype/><D:getetag/>", "1")
        hrefs = []
        for response in root.iter(_tag(DAV, "response")):
            href = response.findtext(_tag(DAV, "href"), default="").strip()
            content_type = response.findtext(f".//{_tag(DAV, 'getcontenttype')}", default="")
            if not href or urljoin(book_url, href).rstrip("/") == book_url.rstrip("/"):
                continue
            if content_type.lower().startswith("text/vcard") or href.lower().endswith(".vcf"):
                hrefs.append(href)
        return hrefs

    def _multiget(self, book_url: str, hrefs: list[str]) -> list[RawCard]:
        refs = "".join(f"<D:href>{escape(h)}</D:href>" for h in hrefs)
        body = (
            f'<?xml version="1.0" encoding="utf-8"?><C:addressbook-multiget {_NS}>'
            f"<D:prop><D:getetag/><C:address-data/></D:prop>{refs}</C:addressbook-multiget>"
        )
        root = self._request("REPORT", book_url, body, "1")
        cards = []
        for response in root.iter(_tag(DAV, "response")):
            data = response.find(f".//{_tag(CARDDAV, 'address-data')}")
            if data is None or not (data.text or "").strip():
                continue
            href = response.findtext(_tag(DAV, "href"), default="").strip()
            etag = response.findtext(f".//{_tag(DAV, 'getetag')}", default="").strip()
            cards.append(
                RawCard(data.text or "", urljoin(book_url, href) if href else None, etag or None)
            )
        return cards

    def fetch_cards(self) -> list[RawCard]:
        principal = self._single_href(self.base_url, DAV, "current-user-principal", "D")
        home = self._single_href(principal, CARDDAV, "addressbook-home-set", "C")
        cards: list[RawCard] = []
        for book in self._addressbooks(home):
            hrefs = self._card_hrefs(book)
            for start in range(0, len(hrefs), MULTIGET_BATCH):
                cards.extend(self._multiget(book, hrefs[start : start + MULTIGET_BATCH]))
        return cards

    def get_card(self, href: str) -> RawCard:
        try:
            response = self.http.get(href)
        except httpx.HTTPError as exc:
            raise CardDavError(f"Could not reach the contacts server: {exc}") from exc
        if response.status_code == 401:
            raise CardDavError(
                "The contacts server rejected the username or app-specific password."
            )
        if response.status_code != 200:
            raise CardDavError(f"GET {href} failed with HTTP {response.status_code}.")
        return RawCard(response.text, href, response.headers.get("ETag"))

    def put_card(self, href: str, text: str, etag: str | None) -> str | None:
        headers = {"Content-Type": "text/vcard; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        try:
            response = self.http.put(href, content=text.encode(), headers=headers)
        except httpx.HTTPError as exc:
            raise CardDavError(f"Could not reach the contacts server: {exc}") from exc
        if response.status_code == 412:
            raise CardChangedError("The contact changed in Apple Contacts since it was read.")
        if response.status_code == 401:
            raise CardDavError(
                "The contacts server rejected the username or app-specific password."
            )
        if response.status_code not in (200, 201, 204):
            raise CardDavError(f"PUT {href} failed with HTTP {response.status_code}.")
        new_etag: str | None = response.headers.get("ETag")
        return new_etag
