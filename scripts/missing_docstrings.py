"""List public functions in orbit/services without a docstring (agents call these).

    uv run python scripts/missing_docstrings.py            # counts per file
    uv run python scripts/missing_docstrings.py --names    # and every function name

Type hints are enforced by mypy (strict on orbit/services); docstrings are not, so this is the
place to see what is left. Exit status is always 0: it is a report, not a gate.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    names = "--names" in sys.argv
    total = missing = 0
    for path in sorted((ROOT / "orbit" / "services").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        lacking = [
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and not node.name.startswith("_")
            and not ast.get_docstring(node)
        ]
        total += sum(
            isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_")
            for n in tree.body
        )
        missing += len(lacking)
        if lacking:
            rel = path.relative_to(ROOT).as_posix()
            print(f"{len(lacking):3d} {rel}" + (": " + ", ".join(lacking) if names else ""))
    print(f"{missing} of {total} public service functions have no docstring.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
