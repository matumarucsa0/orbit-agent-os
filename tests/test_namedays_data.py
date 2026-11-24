"""Sanity and spot checks for data/namedays_{sk,cz}.json (sources: data/README.md)."""

from __future__ import annotations

import calendar
import json
import re
from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[1] / "data"
ALL_DAYS = [
    f"{m:02d}-{d:02d}" for m in range(1, 13) for d in range(1, calendar.monthrange(2000, m)[1] + 1)
]
NAME = re.compile(r"^[A-ZÁÄČĎÉĚÍĹĽŇÓÔŔŘŠŤÚŮÝŽ][a-záäčďéěíĺľňóôŕřšťúůýž]+$")


def load(country: str) -> dict[str, list[str]]:
    data: dict[str, list[str]] = json.loads(
        (DATA / f"namedays_{country}.json").read_text(encoding="utf-8")
    )
    return data


@pytest.mark.parametrize("country", ["sk", "cz"])
def test_structure(country: str) -> None:
    data = load(country)
    assert list(data) == ALL_DAYS  # all 366 days, in calendar order
    for key, names in data.items():
        assert len(names) == len(set(names)), key
        for name in names:
            assert NAME.match(name), f"{key}: {name!r}"


@pytest.mark.parametrize(
    ("day", "names"),
    [
        ("01-01", []),  # Nový rok / Deň vzniku SR
        ("01-02", ["Alexandra", "Sandra", "Karina"]),
        ("02-29", ["Radomír"]),
        ("03-19", ["Jozef"]),
        ("06-24", ["Ján"]),
        ("09-12", ["Mária", "Mia"]),
        ("09-13", ["Ctibor", "Tobiáš", "Tobias"]),
        ("11-02", []),  # Pamiatka zosnulých
        ("11-13", ["Stanislav"]),
        ("12-06", ["Mikuláš", "Nikolas"]),
        ("12-24", ["Adam", "Eva"]),
        ("12-25", []),
        ("12-26", ["Štefan"]),
        ("12-31", ["Silvester"]),
    ],
)
def test_sk_known_dates(day: str, names: list[str]) -> None:
    assert load("sk")[day] == names


@pytest.mark.parametrize(
    ("day", "names"),
    [
        ("01-01", []),  # Nový rok
        ("01-06", []),  # Tři králové
        ("02-29", ["Horymír"]),
        ("03-19", ["Josef"]),
        ("06-24", ["Jan"]),
        ("06-29", ["Petr", "Pavel"]),
        ("07-05", ["Cyril", "Metoděj"]),
        ("09-21", ["Matouš"]),
        ("09-28", ["Václav"]),
        ("12-06", ["Mikuláš"]),
        ("12-24", ["Adam", "Eva"]),
        ("12-25", []),
        ("12-26", ["Štěpán"]),
        ("12-31", ["Silvestr"]),
    ],
)
def test_cz_known_dates(day: str, names: list[str]) -> None:
    assert load("cz")[day] == names
