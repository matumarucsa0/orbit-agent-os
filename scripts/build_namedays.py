"""Build data/namedays_sk.json and data/namedays_cz.json from the downloaded sources.

Not part of the app. Sources and the exact procedure are documented in data/README.md:

    uvx --with pypdf python scripts/build_namedays.py \\
        --sk-pdf Oficialne-kalendarium_2025.pdf --cz-wiki cz_wikipedia.wiki \\
        --check-sk-wiki sk_wikipedia.wiki --check-cz-html cz_kalendar_online.html

Output format: {"MM-DD": ["Name", ...]} for all 366 days; days without a name map to [].
"""

from __future__ import annotations

import argparse
import calendar
import html
import json
import re
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
ALL_DAYS = [
    f"{month:02d}-{day:02d}"
    for month in range(1, 13)
    for day in range(1, calendar.monthrange(2000, month)[1] + 1)  # 2000: leap year
]
CZ_MONTHS = {
    "leden": 1, "únor": 2, "březen": 3, "duben": 4, "květen": 5, "červen": 6,
    "červenec": 7, "srpen": 8, "září": 9, "říjen": 10, "listopad": 11, "prosinec": 12,
}  # fmt: skip
SK_MONTHS = {
    "január": 1, "február": 2, "marec": 3, "apríl": 4, "máj": 5, "jún": 6,
    "júl": 7, "august": 8, "september": 9, "október": 10, "november": 11, "december": 12,
}  # fmt: skip
WIKI_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]|]+)\]\]")


def _split_pair(name: str) -> list[str]:
    """ "Cyril a Metoděj" -> ["Cyril", "Metoděj"]."""
    return [part.strip() for part in re.split(r"\s+a\s+", name) if part.strip()]


def parse_sk_official(pdf_path: Path) -> dict[str, list[str]]:
    """Ministry of Culture 'Oficiálne kalendárium': one line per day, e.g. '2. 1. Alexandra'."""
    from pypdf import PdfReader  # one-off tool dependency (uvx --with pypdf)

    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    days: dict[str, list[str]] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*(\d{1,2})\.\s*(\d{1,2})\.\s*(.*)$", line)
        if not match:
            continue
        key = f"{int(match[2]):02d}-{int(match[1]):02d}"
        names = [n for n in match[3].split() if n != "-"]
        if key in days:
            raise SystemExit(f"SK: duplicate day {key}")
        days[key] = names
    return days


def parse_cz_wikipedia(wikitext: str) -> dict[str, list[str]]:
    """cs.wikipedia 'Jmeniny v Česku' by-month tables.

    Each row lists the civil-calendar name(s) first (pairs joined with ' a '), then extra names
    after commas. Only the part before the first comma is kept.
    """
    days: dict[str, list[str]] = {}
    for line in wikitext.splitlines():
        match = re.match(r"^\s*\|\s*\[\[(\d{1,2})\. ([^\]]+)\]\]\s*\|\|\s*(.*)$", line)
        if not match or match[2] not in CZ_MONTHS:
            continue
        key = f"{CZ_MONTHS[match[2]]:02d}-{int(match[1]):02d}"
        primary = match[3].split(",")[0]
        linked = WIKI_LINK.findall(primary) or [primary]
        days[key] = [name for item in linked for name in _split_pair(item)]
    return days


def parse_sk_wikipedia_official(wikitext: str) -> dict[str, list[str]]:
    """sk.wikipedia 'Meniny na Slovensku': official names are '''bold''', one '#' line per day."""
    days: dict[str, list[str]] = {}
    month = None
    day = 0
    for line in wikitext.splitlines():
        heading = re.match(r"^==+\s*([^=]+?)\s*==+", line)
        if heading:
            name = heading[1].strip().lower()
            month = SK_MONTHS.get(name)
            day = 0
            continue
        if month is None or not line.startswith("#"):
            continue
        day += 1
        bold = re.findall(r"'''(.*?)'''", line)
        days[f"{month:02d}-{day:02d}"] = [n for b in bold for n in WIKI_LINK.findall(b)]
    return days


def parse_cz_kalendar_online(page: str) -> dict[str, list[str]]:
    days: dict[str, list[str]] = {}
    rows = re.findall(
        r">(\d{1,2})\.(\d{1,2})\.</a>.*?<span class=\"svatek\">(.*?)</span>", page, flags=re.S
    )
    for day, month, cell in rows:
        names = [html.unescape(n).strip() for n in re.findall(r">([^<>]+)</a>", cell)]
        days[f"{int(month):02d}-{int(day):02d}"] = names
    return days


def complete(days: dict[str, list[str]], label: str) -> dict[str, list[str]]:
    unknown = set(days) - set(ALL_DAYS)
    if unknown:
        raise SystemExit(f"{label}: unexpected days {sorted(unknown)}")
    return {key: days.get(key, []) for key in ALL_DAYS}


def report_differences(label: str, ours: dict[str, list[str]], other: dict[str, list[str]]) -> None:
    """Days where one of our names is missing from the other source (other may list more)."""
    diffs = [
        (key, names, other.get(key, []))
        for key, names in ours.items()
        if any(name not in other.get(key, []) for name in names)
    ]
    print(f"{label}: {len(diffs)} day(s) differ")
    for key, names, theirs in diffs:
        print(f"  {key}: ours={names} other={theirs}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sk-pdf", type=Path, required=True)
    parser.add_argument("--cz-wiki", type=Path, required=True)
    parser.add_argument("--check-sk-wiki", type=Path)
    parser.add_argument("--check-cz-html", type=Path)
    args = parser.parse_args()

    sk = complete(parse_sk_official(args.sk_pdf), "SK")
    cz = complete(parse_cz_wikipedia(args.cz_wiki.read_text(encoding="utf-8")), "CZ")
    for label, days in (("sk", sk), ("cz", cz)):
        out = DATA_DIR / f"namedays_{label}.json"
        text = json.dumps(days, ensure_ascii=False, indent=1) + "\n"
        out.write_text(text, encoding="utf-8", newline="\n")
        named = sum(1 for names in days.values() if names)
        print(f"wrote {out.name}: {named} named days, {len(days) - named} without a name")

    if args.check_sk_wiki:
        wiki = parse_sk_wikipedia_official(args.check_sk_wiki.read_text(encoding="utf-8"))
        report_differences("SK vs sk.wikipedia (official names)", sk, wiki)
    if args.check_cz_html:
        other = parse_cz_kalendar_online(args.check_cz_html.read_text(encoding="utf-8"))
        report_differences("CZ vs kalendar-online.cz", cz, other)


if __name__ == "__main__":
    main()
