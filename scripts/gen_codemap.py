"""Generate the machine-made docs: docs/CODEMAP.md (tables), ROUTES.md, CONFIG.md, TESTMAP.md.

    uv run python scripts/gen_codemap.py            # rewrite both files
    uv run python scripts/gen_codemap.py --check    # exit 1 when out of date (CI, pre-commit)

Everything between `<!-- BEGIN GENERATED:name -->` and `<!-- END GENERATED:name -->` in
docs/CODEMAP.md comes from the code (module docstrings, the proposal and prompt registries, the job
registry, the migrations). The rest of that file is written by hand. docs/ROUTES.md (every route),
docs/CONFIG.md (every setting and CLI command) and docs/TESTMAP.md (what each test imports) are
generated whole. No line numbers are written, so the output
only changes when a name, a setting or a registry entry changes. Nothing here reads `.env`, opens
the network or touches a database file.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CODEMAP = ROOT / "docs" / "CODEMAP.md"
ROUTES = ROOT / "docs" / "ROUTES.md"
CONFIG = ROOT / "docs" / "CONFIG.md"
TESTMAP = ROOT / "docs" / "TESTMAP.md"
BLOCK = re.compile(
    r"(<!-- BEGIN GENERATED:(?P<name>[a-z_]+) -->\n)(?P<body>.*?)"
    r"(<!-- END GENERATED:(?P=name) -->)",
    re.DOTALL,
)
MAX_NAMES = 4


def _first_line(doc: str | None, limit: int = 110) -> str:
    line = (doc or "").strip().split("\n")[0].strip()
    line = line.replace("|", "/")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# --- modules ---


def modules_block() -> str:
    out: list[str] = []
    packages = ["api", "domain", "services", "integrations", "jobs", "web"]
    groups: list[tuple[str, list[Path]]] = [
        ("orbit/ (top level)", sorted((ROOT / "orbit").glob("*.py")))
    ]
    for pkg in packages:
        groups.append((f"orbit/{pkg}/", sorted((ROOT / "orbit" / pkg).rglob("*.py"))))
    for title, files in groups:
        out += [f"#### `{title}`", "", "| Module | Purpose | Main public names |", "|---|---|---|"]
        for path in files:
            if path.name == "__init__.py" and path.stat().st_size == 0:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = [
                n.name
                for n in tree.body
                if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
                and not n.name.startswith("_")
            ]
            shown = ", ".join(f"`{n}`" for n in names[:MAX_NAMES])
            if len(names) > MAX_NAMES:
                shown += f", +{len(names) - MAX_NAMES}"
            rel = _rel(path).removeprefix("orbit/")
            out.append(f"| `{rel}` | {_first_line(ast.get_docstring(tree))} | {shown} |")
        out.append("")
    return "\n".join(out)


# --- registries ---


def proposal_kinds_block() -> str:
    from orbit.services import inbox  # noqa: F401  (registers every handler)
    from orbit.services.proposal_kinds import _REGISTRY

    rows = ["| Kind | Payload model | Apply function | Defined in |", "|---|---|---|---|"]
    for kind, handler in sorted(_REGISTRY.items(), key=lambda kv: kv[0].value):
        apply = handler.apply
        module = apply.__module__.removeprefix("orbit.")
        rows.append(
            f"| `{kind.value}` | `{handler.payload_model.__name__}` | `{apply.__name__}` "
            f"| `{module.replace('.', '/')}.py` |"
        )
    return "\n".join(rows) + "\n"


def prompts_block() -> str:
    from orbit.integrations.prompts_registry import PROMPTS

    rows = ["| Purpose | Constant (in `orbit/integrations/claude.py`) | Version |", "|---|---|---|"]
    rows += [f"| `{p.purpose}` | `{p.constant}` | {p.version} |" for p in PROMPTS]
    return "\n".join(rows) + "\n"


def _dummy_settings() -> object:
    from orbit.settings import Settings

    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        orbit_api_token="x" * 40,
        orbit_web_password_hash="$argon2id$dummy",
        orbit_secret_key="y" * 40,
        ics_token="z" * 40,
        database_url="sqlite://",
        scheduler_enabled=False,
    )


def jobs_block() -> str:
    import inspect

    from orbit.jobs.scheduler import JOBS

    settings = _dummy_settings()
    rows = [
        "| Job id | What | Interval (defaults) | Needs | Settings switch | Run now |",
        "|---|---|---|---|---|---|",
    ]
    for spec in JOBS:
        interval = ", ".join(f"{v} {k}" for k, v in spec.interval(settings).items())  # type: ignore[arg-type]
        need = (
            spec.available.__name__.removeprefix("_need_")
            if spec.available.__name__ != "_always"
            else ""
        )
        switch = ""
        if spec.enabled.__name__ != "_on":
            match = re.search(
                r"lambda \w+: (.+?)[,)]*\s*$", inspect.getsource(spec.enabled).strip()
            )
            switch = f"`{match.group(1)}`" if match else "yes"
        rows.append(
            f"| `{spec.id}` | {spec.label} | {interval} | {need} | {switch} "
            f"| {'yes' if spec.run_now else ''} |"
        )
    return "\n".join(rows) + "\n"


# --- settings ---


def settings_block() -> str:
    path = ROOT / "orbit" / "settings.py"
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Settings")
    out: list[str] = []
    section = ""
    header = "| Env var | Type | Default | What it does |\n|---|---|---|---|"
    for node in cls.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        name = node.target.id
        if name == "model_config":
            continue
        comments: list[str] = []
        i = node.lineno - 2
        while i >= 0 and lines[i].strip().startswith("#"):
            comments.insert(0, lines[i].strip().lstrip("#").strip())
            i -= 1
        heads = [c for c in comments if c.startswith("---")]
        if heads:
            section = heads[-1].strip("- ").strip()
            out += ["", f"**{section}**", "", header]
            comments = [c for c in comments if not c.startswith("---")]
        env = name.upper()
        default = "required"
        if node.value is not None:
            default = ast.unparse(node.value)
            if isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "Field":
                kws = {k.arg: k.value for k in node.value.keywords}
                if "validation_alias" in kws and isinstance(kws["validation_alias"], ast.Constant):
                    env = str(kws["validation_alias"].value)
                if "default" in kws:
                    default = ast.unparse(kws["default"])
                elif node.value.args:
                    default = ast.unparse(node.value.args[0])
                elif "default_factory" in kws:
                    default = (
                        "(empty)"
                        if ast.unparse(kws["default_factory"]) in {"list", "dict"}
                        else ast.unparse(kws["default_factory"])
                    )
                else:
                    default = "required"
        ann = ast.unparse(node.annotation).replace("Annotated[", "").replace(", NoDecode]", "")
        default = default.replace("|", "/")
        if len(default) > 34:
            default = default[:33] + "…"
        text = " ".join(comments).replace("|", "/")
        if len(text) > 150:
            text = text[:149].rstrip() + "…"
        out.append(f"| `{env}` | `{ann.replace('|', '/')}` | `{default}` | {text} |")
    return "\n".join(out).lstrip("\n") + "\n"


# --- migrations, cli, tests ---


def migrations_block() -> str:
    rows = ["| Revision | Title |", "|---|---|"]
    for path in sorted((ROOT / "migrations" / "versions").glob("*.py")):
        doc = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8")))
        rows.append(f"| `{path.stem}` | {_first_line(doc)} |")
    return "\n".join(rows) + "\n"


def cli_block() -> str:
    tree = ast.parse((ROOT / "orbit" / "cli.py").read_text(encoding="utf-8"))
    rows = ["| Command (`uv run python -m orbit.cli ...`) | What |", "|---|---|"]
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_parser"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            helps = [
                k.value.value
                for k in node.keywords
                if k.arg == "help" and isinstance(k.value, ast.Constant)
            ]
            rows.append(f"| `{node.args[0].value}` | {helps[0] if helps else ''} |")
    return "\n".join(rows) + "\n"


def tests_block() -> str:
    rows = ["| Test file | Orbit modules it imports directly |", "|---|---|"]
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        mods: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.startswith("orbit"):
                    mods.add(
                        node.module.removeprefix("orbit.") if node.module != "orbit" else "orbit"
                    )
                    if node.module in {"orbit.domain", "orbit.services", "orbit.web", "orbit.api"}:
                        mods.discard(node.module.removeprefix("orbit."))
                        mods |= {
                            f"{node.module.removeprefix('orbit.')}.{a.name}" for a in node.names
                        }
            elif isinstance(node, ast.Import):
                mods |= {
                    a.name.removeprefix("orbit.") for a in node.names if a.name.startswith("orbit.")
                }
        shown = ", ".join(f"`{m}`" for m in sorted(mods)) or "(app fixture only)"
        rows.append(f"| `{path.name}` | {shown} |")
    return "\n".join(rows) + "\n"


BLOCKS = {
    "modules": modules_block,
    "proposal_kinds": proposal_kinds_block,
    "prompts": prompts_block,
    "jobs": jobs_block,
    "migrations": migrations_block,
}


# --- routes ---


def _router_info(tree: ast.Module) -> tuple[str, str]:
    """The `router = APIRouter(...)` prefix and its source (to look for `require_login`)."""
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "router" for t in node.targets)
            and isinstance(node.value, ast.Call)
        ):
            prefix = ""
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = str(kw.value.value)
            return prefix, ast.unparse(node.value)
    return "", ""


_AUTH_BY_FILE = {
    "api/actions.py": "signed single-use token in the URL",
    "api/health.py": "none",
    "api/claude_worker.py": "ORBIT_REVIEW_TOKEN",
    "api/review_agent.py": "ORBIT_REVIEW_TOKEN",
    "web/calendar.py": "ICS_TOKEN in the URL",
    "web/auth.py": "none (login form)",
}


def routes_text() -> str:
    """Routes found by reading the `@router.<method>("path")` decorators (no app is built)."""
    api_router_src = (ROOT / "orbit" / "api" / "router.py").read_text(encoding="utf-8")
    under_api_v1 = set(re.findall(r"router\.include_router\((\w+)\.router\)", api_router_src))
    rows: list[tuple[str, str, str, str, str]] = []
    for area in ("api", "web"):
        for path in sorted((ROOT / "orbit" / area).glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            prefix, ctor = _router_info(tree)
            rel = f"{area}/{path.name}"
            if area == "api" and path.stem in under_api_v1:
                base, auth = "/api/v1" + prefix, "API bearer token"
            else:
                base = prefix
                auth = _AUTH_BY_FILE.get(rel) or ("web session" if "require_login" in ctor else "?")
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    continue
                for dec in node.decorator_list:
                    if (
                        isinstance(dec, ast.Call)
                        and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name)
                        and dec.func.value.id == "router"
                        and dec.func.attr in {"get", "post", "put", "patch", "delete"}
                        and dec.args
                        and isinstance(dec.args[0], ast.Constant)
                    ):
                        rows.append(
                            (
                                area,
                                base + str(dec.args[0].value),
                                dec.func.attr.upper(),
                                f"orbit/{rel}::{node.name}",
                                auth,
                            )
                        )
    rows.sort()
    out = [
        "# Routes",
        "",
        "Generated by `scripts/gen_codemap.py` from the `@router.<method>(...)` decorators (do not",
        "edit; `make codemap` rewrites it). Paths are as served; the auth column is what guards",
        "the router (see `orbit/api/AGENTS.md` and `orbit/web/AGENTS.md`). `?` means the script",
        "could not tell: check the router's `dependencies`.",
        "",
    ]
    for area, title in (("api", "JSON API and workers"), ("web", "Web pages, feeds and actions")):
        out += [f"## {title}", "", "| Method | Path | Handler | Auth |", "|---|---|---|---|"]
        out += [f"| {m} | `{p}` | `{h}` | {a} |" for ar, p, m, h, a in rows if ar == area]
        out.append("")
    return "\n".join(out)


def config_text() -> str:
    return (
        "# Configuration map\n\n"
        "Generated by `scripts/gen_codemap.py` (do not edit; `make codemap` rewrites it).\n"
        "Every field of `orbit/settings.py::Settings` as an environment variable (or `.env`\n"
        "entry), its type, default and what it does; `.env.example` has the commented\n"
        "template. Secrets are `SecretStr` and are never printed. The defaults shown are the\n"
        "code defaults; values edited on `/settings/schedule` override some of them at run time\n"
        "(`services/schedule_config.py::OverrideStore`).\n\n"
        + settings_block()
        + "\n## CLI commands\n\n"
        + cli_block()
    )


def testmap_text() -> str:
    return (
        "# Test map\n\n"
        "Generated by `scripts/gen_codemap.py` (do not edit; `make codemap` rewrites it).\n"
        "For each test file, the `orbit` modules it imports directly. `scripts/select_tests.py`\n"
        "follows these imports transitively to choose the tests a change can affect\n"
        "(`make changed`). The areas and fixtures are explained in `docs/CODEMAP.md`\n"
        "section 7.\n\n" + tests_block()
    )


# --- driver ---


def render_codemap(current: str) -> str:
    def fill(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in BLOCKS:
            raise SystemExit(f"docs/CODEMAP.md has an unknown generated block: {name}")
        return f"{match.group(1)}\n{BLOCKS[name]().strip()}\n\n{match.group(4)}"

    return BLOCK.sub(fill, current)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail when the files are stale")
    args = parser.parse_args()
    wanted = {
        CODEMAP: render_codemap(CODEMAP.read_text(encoding="utf-8")),
        ROUTES: routes_text(),
        CONFIG: config_text(),
        TESTMAP: testmap_text(),
    }
    stale = [
        p for p, text in wanted.items() if not p.exists() or p.read_text(encoding="utf-8") != text
    ]
    if args.check:
        if stale:
            names = ", ".join(_rel(p) for p in stale)
            print(
                f"Out of date: {names}. Run `make codemap` and commit the result.", file=sys.stderr
            )
            return 1
        print("docs/CODEMAP.md and docs/ROUTES.md are current.")
        return 0
    for path, text in wanted.items():
        path.write_text(text, encoding="utf-8")
    print("Wrote " + ", ".join(_rel(p) for p in wanted))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
