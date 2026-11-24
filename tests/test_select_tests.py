"""The CI test selector (scripts/select_tests.py): it must never skip a test a change can break."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("select_tests", ROOT / "scripts/select_tests.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["select_tests"] = module
    spec.loader.exec_module(module)
    return module


sel = load()


def test_docs_only_runs_nothing() -> None:
    assert sel.select(["docs/SPEC.md", "CLAUDE.md"]) == "NONE"


def test_shared_infrastructure_runs_everything() -> None:
    for f in (
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "orbit/domain/school.py",
        "migrations/versions/x.py",
    ):
        assert sel.select([f]) == "ALL", f


def test_unknown_file_runs_everything() -> None:
    assert sel.select(["something/new.bin"]) == "ALL"


def test_changed_service_selects_its_tests_and_their_dependents() -> None:
    result = sel.select(["orbit/services/school_map.py"])
    assert isinstance(result, list)
    assert "tests/test_school_map.py" in result
    assert "tests/test_school_endpoints.py" in result  # imports it through a router
    assert "tests/test_action_tokens.py" not in result


def test_changed_test_selects_itself() -> None:
    assert sel.select(["tests/test_cadence.py"]) == ["tests/test_cadence.py"]


def test_template_selects_endpoint_tests_only() -> None:
    result = sel.select(["orbit/web/templates/school/map.html"])
    assert isinstance(result, list)
    assert "tests/test_school_map.py" in result
    assert "tests/test_bank_parsers.py" not in result


def test_unused_fixture_runs_everything() -> None:
    assert sel.select(["tests/fixtures/" + "no-test-mentions" + "-this-file.csv"]) == "ALL"


def test_every_test_file_is_reachable_from_itself() -> None:
    known = sel.project_modules()
    deps = sel.dependencies(known)
    for path, reach in deps.items():
        assert sel.module_of(path) in reach
