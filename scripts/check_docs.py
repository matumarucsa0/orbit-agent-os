"""Check the instruction files and docs for dead paths, dead links and broken agent config.

    uv run python scripts/check_docs.py          # exit 1 and print one line per problem

What it checks (the point is that docs an agent reads cannot quietly go stale):
- every relative markdown link resolves;
- every backticked repo path (like `orbit/services/inbox.py` or `docs/CODEMAP.md`) exists,
  and for `path::name` the name appears in that file; placeholders (`<x>`, `*`, `NNNN`) are skipped;
- every `make <target>` mentioned exists in the Makefile;
- size limits of the instruction files (AGENTS.md, nested AGENTS.md, CLAUDE.md);
- `.claude/`: valid settings.json, hook scripts exist and are executable, every skill has a
  SKILL.md with `name` equal to its directory and a description, agents and commands have a
  description, and the files are not git-ignored.
No network, no database, no secrets read.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TOP_DIRS = (
    "orbit/",
    "docs/",
    "tests/",
    "scripts/",
    "evals/",
    "migrations/",
    "deploy/",
    "docker/",
    "data/",
    ".claude/",
    ".github/",
)
ROOT_FILES = {
    "AGENTS.md",
    "CLAUDE.md",
    "STATUS.md",
    "README.md",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "LICENSE",
    "Makefile",
    "pyproject.toml",
    "alembic.ini",
    ".env.example",
    ".mcp.json",
    ".claudeignore",
    ".pre-commit-config.yaml",
    ".pre-publish-patterns.example",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "Dockerfile",
}
# Files the docs name on purpose although they are git-ignored and absent from a clean clone.
NEVER_COMMITTED = {".claude/settings.local.json"}
# Files whose content is generated or historical: not checked for stale mentions.
SKIP = {"docs/ROUTES.md"}
# Historical or aspirational docs mention files that never existed or moved; checked only for links.
LINKS_ONLY = {"docs/SPEC.md", "docs/ROADMAP.md", "docs/DECISIONS.md", "CHANGELOG.md"}
PATH_RE = re.compile(r"`([A-Za-z0-9_./\-]+(?:::[A-Za-z_][A-Za-z0-9_.]*)?)`")
LINK_RE = re.compile(r"\]\(([^)\s#]+)(?:#[^)]*)?\)")
MAKE_RE = re.compile(r"`make ([a-z][a-z0-9_-]*)")
LIMITS = {"AGENTS.md": 120, "CLAUDE.md": 40}
NESTED_LIMITS = (15, 60)


def doc_files() -> list[Path]:
    found = {ROOT / n for n in ROOT_FILES if n.endswith(".md")}
    found |= set((ROOT / "docs").glob("*.md"))
    found |= set(ROOT.glob("orbit/**/AGENTS.md"))
    for sub in ("migrations", "tests", "evals", "scripts"):
        found |= set((ROOT / sub).glob("AGENTS.md"))
    found |= set((ROOT / ".claude").rglob("*.md"))
    return sorted(p for p in found if p.exists())


def makefile_targets() -> set[str]:
    path = ROOT / "Makefile"
    if not path.exists():
        return set()
    return set(re.findall(r"^([a-z][a-z0-9_-]*):", path.read_text(encoding="utf-8"), re.M))


def looks_like_repo_path(token: str) -> bool:
    if any(c in token for c in "*<>{}") or "NNNN" in token or token.endswith("/"):
        return False
    if token in NEVER_COMMITTED:
        return False
    if token in ROOT_FILES:
        return True
    return token.startswith(TOP_DIRS) and "." in token.rsplit("/", 1)[-1]


def check_text(path: Path, rel: str, targets: set[str]) -> list[str]:
    problems: list[str] = []
    text = path.read_text(encoding="utf-8")
    # The generated tables are right by construction (and checked by gen_codemap.py --check);
    # keep the line count so reported line numbers stay true.
    text = re.sub(
        r"<!-- BEGIN GENERATED:(\w+) -->.*?<!-- END GENERATED:\1 -->",
        lambda m: "\n" * m.group(0).count("\n"),
        text,
        flags=re.S,
    )
    in_fence = False
    for number, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        for link in LINK_RE.findall(line):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / link).resolve().exists():
                problems.append(f"{rel}:{number}: dead link ({link})")
        if rel in LINKS_ONLY:
            continue
        for token in PATH_RE.findall(line):
            ref, _, symbol = token.partition("::")
            if looks_like_repo_path(ref):
                target = ROOT / ref
                if not target.exists():
                    problems.append(f"{rel}:{number}: path does not exist (`{ref}`)")
                elif symbol and target.is_file():
                    name = symbol.split(".")[0]
                    if not re.search(rf"\b{re.escape(name)}\b", target.read_text(encoding="utf-8")):
                        problems.append(f"{rel}:{number}: `{name}` not found in {ref}")
        if not in_fence or line.lstrip().startswith(("make ", "$ make ")):
            for target_name in MAKE_RE.findall(line):
                if target_name not in targets:
                    problems.append(f"{rel}:{number}: no Makefile target `{target_name}`")
    return problems


def frontmatter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    block = text.split("---\n", 2)[1]
    out: dict[str, str] = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
    return out


def check_sizes() -> list[str]:
    problems: list[str] = []
    for name, limit in LIMITS.items():
        path = ROOT / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif len(path.read_text(encoding="utf-8").splitlines()) > limit:
            problems.append(f"{name}: longer than {limit} lines (keep it an index)")
    low, high = NESTED_LIMITS
    for path in sorted(ROOT.glob("**/AGENTS.md")):
        rel = path.relative_to(ROOT).as_posix()
        if rel == "AGENTS.md" or ".venv" in rel:
            continue
        count = len(path.read_text(encoding="utf-8").splitlines())
        if not low <= count <= high:
            problems.append(f"{rel}: {count} lines, nested AGENTS.md files stay {low}-{high}")
    return problems


def check_claude_dir() -> list[str]:
    problems: list[str] = []
    claude = ROOT / ".claude"
    settings = claude / "settings.json"
    if not settings.exists():
        return [".claude/settings.json: missing"]
    try:
        data = json.loads(settings.read_text(encoding="utf-8"))
    except ValueError as exc:
        return [f".claude/settings.json: invalid JSON ({exc})"]
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for hook in group.get("hooks", []):
                for match in re.findall(r"\.claude/hooks/[\w.\-]+", hook.get("command", "")):
                    script = ROOT / match
                    if not script.exists():
                        problems.append(
                            f".claude/settings.json: {event} hook script {match} missing"
                        )
                    elif not script.stat().st_mode & 0o111:
                        problems.append(f"{match}: not executable")
    for skill_md in sorted((claude / "skills").glob("*/SKILL.md")):
        meta = frontmatter(skill_md)
        if meta.get("name") != skill_md.parent.name or not meta.get("description"):
            problems.append(
                f"{skill_md.relative_to(ROOT)}: frontmatter needs name and a description"
            )
    for sub in ("agents", "commands"):
        for md in sorted((claude / sub).glob("*.md")):
            if not frontmatter(md).get("description"):
                problems.append(f"{md.relative_to(ROOT)}: frontmatter needs a description")
    ignored = subprocess.run(
        ["git", "check-ignore", ".claude/settings.json", ".claude/skills/x/SKILL.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    ).stdout.split()
    if ignored:
        problems.append(f".gitignore hides committed agent config: {', '.join(ignored)}")
    return problems


def main() -> int:
    targets = makefile_targets()
    problems = check_sizes() + check_claude_dir()
    for path in doc_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in SKIP:
            continue
        problems += check_text(path, rel, targets)
    for problem in problems:
        print(problem)
    if problems:
        print(f"{len(problems)} problem(s) in the docs and agent config.", file=sys.stderr)
        return 1
    print("Docs and agent config: no dead paths, links or targets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
