"""CardDAV client against a mocked iCloud (httpx.MockTransport), modelled on iCloud's responses."""

from __future__ import annotations

import httpx
import pytest

from orbit.integrations.carddav import CardDavClient, CardDavError
from orbit.integrations.vcard import parse_vcards

BASE = "https://contacts.icloud.com"
HOME = "https://p42-contacts.icloud.com:443/1234567/carddav/"
HOME_PATH = "/1234567/carddav/"
BOOK_PATH = HOME_PATH + "card/"

PRINCIPAL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:"><response><href>/</href><propstat><prop>
<current-user-principal><href>/1234567/principal/</href></current-user-principal>
</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"""

HOME_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:"><response><href>/1234567/principal/</href><propstat><prop>
<addressbook-home-set xmlns="urn:ietf:params:xml:ns:carddav"><href xmlns="DAV:">{HOME}</href></addressbook-home-set>
</prop><status>HTTP/1.1 200 OK</status></propstat></response></multistatus>"""

BOOKS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:">
<response><href>/1234567/carddav/</href><propstat><prop><resourcetype><collection/></resourcetype></prop>
<status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/1234567/carddav/card/</href><propstat><prop><resourcetype><collection/>
<addressbook xmlns="urn:ietf:params:xml:ns:carddav"/></resourcetype></prop>
<status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""

CARDS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:">
<response><href>/1234567/carddav/card/</href><propstat><prop><getcontenttype>httpd/unix-directory</getcontenttype>
</prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/1234567/carddav/card/aaa.vcf</href><propstat><prop><getcontenttype>text/vcard</getcontenttype>
<getetag>"e1"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/1234567/carddav/card/bbb.vcf</href><propstat><prop><getcontenttype>text/vcard; charset=utf-8</getcontenttype>
<getetag>"e2"</getetag></prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""

VCARD_A = "BEGIN:VCARD\nVERSION:3.0\nFN:Patrik Veselý\nUID:aaa\nEND:VCARD\n"
VCARD_B = "BEGIN:VCARD\nVERSION:3.0\nFN:Terézia &amp; Co\nUID:bbb\nEND:VCARD\n"

MULTIGET_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<multistatus xmlns="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
<response><href>/1234567/carddav/card/aaa.vcf</href><propstat><prop><getetag>"e1"</getetag>
<C:address-data>{VCARD_A}</C:address-data></prop><status>HTTP/1.1 200 OK</status></propstat></response>
<response><href>/1234567/carddav/card/bbb.vcf</href><propstat><prop><getetag>"e2"</getetag>
<C:address-data>{VCARD_B}</C:address-data></prop><status>HTTP/1.1 200 OK</status></propstat></response>
</multistatus>"""


def icloud_handler(requests: list[httpx.Request]):
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = request.content.decode()
        path = request.url.path  # httpx drops the default :443 that iCloud puts in hrefs
        if request.method == "PROPFIND" and "current-user-principal" in body:
            return httpx.Response(207, text=PRINCIPAL_XML)
        if request.method == "PROPFIND" and "addressbook-home-set" in body:
            assert str(request.url) == f"{BASE}/1234567/principal/"
            return httpx.Response(207, text=HOME_XML)
        if request.method == "PROPFIND" and path == HOME_PATH:
            return httpx.Response(207, text=BOOKS_XML)
        if request.method == "PROPFIND" and path == BOOK_PATH:
            return httpx.Response(207, text=CARDS_XML)
        if request.method == "REPORT" and path == BOOK_PATH:
            assert "/1234567/carddav/card/aaa.vcf" in body
            assert "/1234567/carddav/card/bbb.vcf" in body
            return httpx.Response(207, text=MULTIGET_XML)
        return httpx.Response(404)

    return handle


def test_fetch_cards_discovery_flow() -> None:
    requests: list[httpx.Request] = []
    client = CardDavClient(
        BASE, httpx.Client(transport=httpx.MockTransport(icloud_handler(requests)))
    )

    cards = client.fetch_cards()

    names = [card.formatted_name for raw in cards for card in parse_vcards(raw.text)]
    assert names == ["Patrik Veselý", "Terézia & Co"]
    assert [(c.href, c.etag) for c in cards] == [
        ("https://p42-contacts.icloud.com:443/1234567/carddav/card/aaa.vcf", '"e1"'),
        ("https://p42-contacts.icloud.com:443/1234567/carddav/card/bbb.vcf", '"e2"'),
    ]
    assert [r.method for r in requests] == ["PROPFIND"] * 4 + ["REPORT"]
    assert requests[0].headers["Depth"] == "0"
    assert requests[2].headers["Depth"] == "1"


def test_unauthorized() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(401))
    client = CardDavClient(BASE, httpx.Client(transport=transport))
    with pytest.raises(CardDavError, match="app-specific password"):
        client.fetch_cards()


def test_network_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    client = CardDavClient(BASE, httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(CardDavError, match="Could not reach"):
        client.fetch_cards()


def test_missing_principal() -> None:
    empty = '<?xml version="1.0"?><multistatus xmlns="DAV:"></multistatus>'
    transport = httpx.MockTransport(lambda request: httpx.Response(207, text=empty))
    client = CardDavClient(BASE, httpx.Client(transport=transport))
    with pytest.raises(CardDavError, match="current-user-principal"):
        client.fetch_cards()


def test_invalid_xml() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(207, text="<nope"))
    client = CardDavClient(BASE, httpx.Client(transport=transport))
    with pytest.raises(CardDavError, match="Invalid XML"):
        client.fetch_cards()


def test_multiget_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("orbit.integrations.carddav.MULTIGET_BATCH", 1)
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "REPORT":
            requests.append(request)
            return httpx.Response(207, text=MULTIGET_XML)
        return icloud_handler([])(request)

    client = CardDavClient(BASE, httpx.Client(transport=httpx.MockTransport(handle)))
    client.fetch_cards()
    assert len(requests) == 2
