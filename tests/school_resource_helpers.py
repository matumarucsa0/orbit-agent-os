"""Fakes shared by the school resource tests. No test touches the network."""

from __future__ import annotations

import io
from collections.abc import Callable

from pypdf import PdfWriter

from orbit.integrations.claude import SchoolReadOutput, SchoolReadRequest
from orbit.integrations.resources import FetchResult


def pdf_with_text(text: str) -> bytes:
    """A small valid PDF with one line of text (xref offsets computed)."""
    stream = f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode()
    bodies = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>",
        b"<</Length " + str(len(stream)).encode() + b">>\nstream\n" + stream + b"\nendstream",
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(bodies, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(bodies) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<</Size {len(bodies) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def page(body: str, charset: str = "utf-8") -> FetchResult:
    return FetchResult(
        "ok",
        body=body.encode(charset),
        content_type=f"text/html; charset={charset}",
        etag='"v1"',
    )


class FakeFetcher:
    """Answers by URL; `pages[url]` may be a FetchResult or a callable that builds one."""

    def __init__(self, pages: dict[str, FetchResult | Callable[[], FetchResult]] | None = None):
        self.pages = pages or {}
        self.calls: list[tuple[str, str | None]] = []

    def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.calls.append((url, etag))
        found = self.pages.get(url)
        if found is None:
            return FetchResult("error", error="HTTP 404.")
        return found() if callable(found) else found


class FakeReader:
    def __init__(self, *answers: SchoolReadOutput | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[SchoolReadRequest] = []

    def read_school(self, request: SchoolReadRequest) -> SchoolReadOutput:
        self.requests.append(request)
        answer = (
            self.answers.pop(0)
            if self.answers
            else SchoolReadOutput(deadlines=[], facts=[], links=[])
        )
        if isinstance(answer, Exception):
            raise answer
        return answer
