"""Pick the tests a change can affect (CI, pull requests only).

Reads the changed files from `git diff <merge-base> HEAD` and prints `ALL`, `NONE` or a list of test
files, one per line. No dependency: it follows the static imports of every test file, so a test runs
when any module it reaches changed. Anything it cannot place runs everything. The push to `main`
always runs the full suite (see ci.yml).

    uv run python scripts/select_tests.py origin/main
    uv run python scripts/select_tests.py --worktree     # uncommitted + untracked (make changed)
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# A change here can break anything: run the full suite.
RUN_ALL = (
    "pyproject.toml",
    "uv.lock",
    "alembic.ini",
    ".gitattributes",
    ".github/",
    "migrations/",
    "data/",
    "scripts/select_tests.py",
    "tests/conftest.py",
    "tests/__init__.py",
    "orbit/domain/",
    "orbit/db.py",
    "orbit/settings.py",
    "orbit/app.py",
    "orbit/main.py",
    "orbit/deps.py",
)
# These cannot change what a test sees.
RUN_NONE = (
    "docs/",
    "README",
    "CLAUDE.md",
    "AGENTS.md",
    "STATUS.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "Makefile",
    ".claudeignore",
    ".pre-publish-patterns.example",
    ".pre-commit-config.yaml",
    "scripts/scan-personal.sh",
    "LICENSE",
    ".claude/",
    "deploy/",
    "docker/",
    "Dockerfile",
    ".dockerignore",
    ".gitignore",
    "docker-compose",
    "Caddyfile",
    ".env.example",
)
ENDPOINT_FIXTURES = {"client", "api", "web", "app"}
# Reached through the web fixtures only: not walked, the routers are matched by name instead.
NOT_WALKED = {"orbit.app", "orbit.main"}


def module_of(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def project_modules() -> dict[str, Path]:
    found: dict[str, Path] = {}
    for top in ("orbit", "tests"):
        for path in (ROOT / top).rglob("*.py"):
            found[module_of(path)] = path
    return found


def imports_of(path: Path, known: dict[str, Path]) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        for name in names:
            # `import a.b.c` also loads a and a.b.
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                if ".".join(parts[:i]) in known:
                    out.add(".".join(parts[:i]))
    return out


def closure(start: str, graph: dict[str, set[str]]) -> set[str]:
    seen = {start}
    todo = [start]
    while todo:
        for nxt in graph.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                if nxt not in NOT_WALKED:
                    todo.append(nxt)
    return seen


def uses_endpoints(path: Path) -> bool:
    """True when a test function (or a fixture) takes the client, api, web or app fixture."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and ENDPOINT_FIXTURES & {
            a.arg for a in node.args.args + node.args.kwonlyargs
        }:
            return True
    return False


def tokens(stem: str) -> set[str]:
    return {t for t in re.split(r"[_\W]+", stem) if len(t) > 2} - {
        "test",
        "web",
        "api",
        "endpoints",
        "tests",
    }


def dependencies(known: dict[str, Path]) -> dict[Path, set[str]]:
    graph = {m: imports_of(p, known) for m, p in known.items()}
    routers = [m for m in known if m.startswith(("orbit.web.", "orbit.api."))]
    deps: dict[Path, set[str]] = {}
    for module, path in known.items():
        if not (module.startswith("tests.test_")):
            continue
        reach = closure(module, graph)
        if uses_endpoints(path):
            # Endpoint tests reach their pages through the app fixture: add the routers named alike.
            mine = tokens(path.stem)
            for router in routers:
                if mine & tokens(router.rsplit(".", 1)[1]):
                    reach |= closure(router, graph)
        deps[path] = reach
    return deps


def select(changed: list[str], known: dict[str, Path] | None = None) -> list[str] | str:
    """`ALL`, `NONE` or the sorted test paths (relative to the repository root)."""
    known = known or project_modules()
    deps = dependencies(known)
    all_tests = sorted(str(p.relative_to(ROOT)) for p in deps)
    chosen: set[Path] = set()
    for f in changed:
        if f.startswith(RUN_ALL):
            return "ALL"
        if f.startswith(RUN_NONE):
            continue
        path = ROOT / f
        if f.startswith("tests/test_") and f.endswith(".py"):
            chosen.add(path)
        elif f.endswith(".py") and f.startswith(("orbit/", "tests/")):
            if not path.exists():  # deleted module: its tests may fail on the import
                return "ALL"
            module = module_of(path)
            chosen |= {t for t, reach in deps.items() if module in reach}
        elif f.startswith("orbit/web/") and not f.endswith(".py"):  # templates, static files
            chosen |= {t for t in deps if uses_endpoints(t)}
        elif f.startswith("tests/fixtures/"):
            name = Path(f).name
            hits = {t for t in deps if name in t.read_text(encoding="utf-8")}
            if not hits:
                return "ALL"
            chosen |= hits
        elif f.startswith("scripts/") and f.endswith((".py", ".sh")):
            chosen |= {t for t in deps if Path(f).stem in t.read_text(encoding="utf-8")}
        else:
            return "ALL"  # unknown kind of file: be safe
    result = sorted(str(p.relative_to(ROOT)) for p in chosen if p.exists() and p in deps)
    if not result:
        return "NONE"
    return "ALL" if len(result) == len(all_tests) else result


def changed_files(base: str) -> list[str]:
    merge_base = subprocess.run(
        ["git", "merge-base", base, "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    diff = subprocess.run(
        ["git", "diff", "--name-only", merge_base, "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [line for line in diff.splitlines() if line]


def worktree_files() -> list[str]:
    """Files changed since HEAD, staged or not, plus untracked ones (not ignored)."""
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return sorted({line for line in (tracked + untracked).splitlines() if line})


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: select_tests.py <base-ref> | --worktree", file=sys.stderr)
        return 2
    changed = worktree_files() if sys.argv[1] == "--worktree" else changed_files(sys.argv[1])
    result = select(changed)
    print(result if isinstance(result, str) else "\n".join(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
