"""Admin CLI: `uv run python -m orbit.cli <command>`."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from getpass import getpass
from pathlib import Path

import httpx
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from orbit.db import make_engine, make_sessionmaker
from orbit.domain.enums import ProposalKind, ProposalStatus
from orbit.domain.inbox import Proposal
from orbit.integrations.ntfy import (
    NtfyClient,
    NtfyError,
    PushMessage,
    http_action,
    view_action,
)
from orbit.services import export, usage
from orbit.services.action_tokens import action_url
from orbit.services.capture import fallback_payload
from orbit.services.db_copy import copy_database
from orbit.services.errors import InvalidInputError
from orbit.services.passwords import hash_password
from orbit.services.smoke import run_checks
from orbit.settings import get_settings

MIN_PASSWORD_LENGTH = 10


def _hash_password() -> int:
    password = getpass("Web UI password: ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1
    if getpass("Repeat: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    # Single quotes keep the `$` characters literal for both python-dotenv and docker compose.
    print(f"ORBIT_WEB_PASSWORD_HASH='{hash_password(password)}'")
    return 0


PUSH_TEST_TEXT = "Orbit push test: tap Reject on the push notification to remove this item."


def _test_push(with_action: bool) -> int:
    """Send one push straight to ntfy (not recorded, ignores quiet hours) to check the setup.

    With an action: a throwaway Inbox item and a signed Reject button on the push, so tapping it
    checks the whole round trip phone -> /a/{token} -> database.
    """
    settings = get_settings()
    if not settings.ntfy_topic:
        print("NTFY_TOPIC isn't set.", file=sys.stderr)
        return 1
    token = settings.ntfy_token.get_secret_value() if settings.ntfy_token else None
    client = NtfyClient(settings.ntfy_url, settings.ntfy_topic, token)
    engine = make_engine(settings.database_url) if with_action else None
    session = make_sessionmaker(engine)() if engine is not None else None
    try:
        if session is None:
            message = PushMessage(
                "Orbit test",
                "Pushes work. Tap Inbox to open Orbit.",
                tags=("white_check_mark",),
                actions=(view_action("Inbox", f"{settings.orbit_url}/inbox"),),
            )
        else:
            proposal = Proposal(
                kind=ProposalKind.CAPTURE,
                status=ProposalStatus.PENDING,
                summary="Push test (reject me)",
                payload_json=fallback_payload(PUSH_TEST_TEXT, []).model_dump(mode="json"),
                source_type="push_test",
            )
            session.add(proposal)
            session.flush()
            secret = settings.orbit_secret_key.get_secret_value()
            message = PushMessage(
                "Orbit test: tap Reject",
                "It removes a test item from the Inbox. If the item is still there afterwards, "
                "the phone couldn't reach Orbit.",
                click=f"{settings.orbit_url}/inbox/{proposal.id}",
                tags=("white_check_mark",),
                actions=(
                    http_action(
                        "Reject", action_url(settings.orbit_url, secret, proposal.id, "reject")
                    ),
                    view_action("Inbox", f"{settings.orbit_url}/inbox"),
                ),
            )
        client.send(message)
        if session is not None:
            session.commit()  # only once the push is out: no orphan test item otherwise
    except NtfyError as exc:
        print(f"Push failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
        if session is not None:
            session.close()
        if engine is not None:
            engine.dispose()
    print(f"Sent a test push to {settings.ntfy_url} (topic {settings.ntfy_topic}).")
    if with_action:
        print(f"Tap Reject on it; then {settings.orbit_url}/inbox shouldn't list the test item.")
    return 0


def _copy_db(source_url: str, target_url: str | None) -> int:
    """Copy all data into an empty, migrated database (the default: DATABASE_URL)."""
    target_url = target_url or get_settings().database_url
    if make_url(source_url) == make_url(target_url):
        print("Source and target are the same database.", file=sys.stderr)
        return 1
    source, target = make_engine(source_url), make_engine(target_url)
    try:
        counts = copy_database(source, target)
    except InvalidInputError as exc:
        print(f"Nothing copied: {exc}", file=sys.stderr)
        return 1
    finally:
        source.dispose()
        target.dispose()
    for table, count in counts.items():
        print(f"{table:20} {count:6}")
    print(f"Copied {sum(counts.values())} rows into {make_url(target_url)!r}.")
    return 0


def _usage(months: int) -> int:
    """Claude calls and tokens per month and purpose (the app writes them once a minute)."""
    settings = get_settings()
    engine = make_engine(settings.database_url)
    today = datetime.now(UTC).astimezone(settings.timezone).date()
    first = usage.months_back(today, months - 1)
    try:
        with make_sessionmaker(engine)() as session:
            rows = usage.totals(session, first, today, settings.timezone)
    finally:
        engine.dispose()
    if not rows:
        print("No Claude calls recorded.")
        return 0
    print(f"{'month':8} {'purpose':14} {'calls':>6} {'failed':>6} {'in':>8} {'out':>8}")
    for r in rows:
        print(
            f"{r.month:%Y-%m}  {r.purpose:14} {r.calls:6} {r.failed:6}"
            f" {usage.format_tokens(r.input_tokens):>8} {usage.format_tokens(r.output_tokens):>8}"
        )
    return 0


def _export(output: str | None) -> int:
    """Write everything in the database as JSON (stdout, or a file)."""
    engine = make_engine(get_settings().database_url)
    try:
        with make_sessionmaker(engine)() as session:
            text = export.to_json(export.export_data(session))
    finally:
        engine.dispose()
    if output is None:
        sys.stdout.write(text + "\n")
        return 0
    Path(output).write_text(text + "\n", encoding="utf-8")
    print(f"Exported to {output} (it holds personal data: keep it private).", file=sys.stderr)
    return 0


def _smoke_test(url: str | None) -> int:
    settings = get_settings()
    base_url = (url or settings.orbit_url).rstrip("/")
    with httpx.Client(timeout=15) as client:
        results = run_checks(
            client,
            base_url,
            settings.orbit_api_token.get_secret_value(),
            settings.ics_token.get_secret_value(),
        )
    print(f"Checking {base_url}")
    for result in results:
        print(
            f"  {'ok  ' if result.ok else 'FAIL'}  {result.name}"
            + (f": {result.detail}" if result.detail else "")
        )
    failed = sum(not result.ok for result in results)
    print("All checks passed." if not failed else f"{failed} check(s) failed.")
    return 1 if failed else 0


def _prompts_snapshot() -> int:
    """Write tests/prompt_hashes.json from the current prompt texts and versions."""
    import json
    from pathlib import Path

    from orbit.integrations.prompts_registry import current_snapshot

    path = Path(__file__).resolve().parents[1] / "tests" / "prompt_hashes.json"
    path.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n")
    print(f"Wrote {path}. Add a line to docs/PROMPTS.md for any prompt whose version changed.")
    return 0


def _seed_demo(force: bool) -> int:
    """Fill an empty, migrated database with the fictional demo persona."""
    from orbit.services import demo  # keeps the other commands free of the demo code

    settings = get_settings()
    engine = make_engine(settings.database_url)
    try:
        report = demo.seed_demo(
            make_sessionmaker(engine),
            tz=settings.timezone,
            force=force,
            context_budget_chars=settings.context_budget_chars,
        )
    except demo.NotEmptyError as exc:
        print(f"Nothing seeded: {exc}", file=sys.stderr)
        return 1
    except OperationalError as exc:
        print(
            f"The database isn't ready ({exc.__class__.__name__}): run `alembic upgrade head`.",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()
    if report.already_seeded:
        print("The demo data is already there; nothing changed.")
    else:
        print(f"Seeded the demo persona {demo.PERSONA}: {report.line()}.")
    print("Run it with Claude answering offline (no API key, no network):")
    print(f"  ORBIT_CLAUDE_MODE=fake OWNER_NAME={demo.OWNER_FIRST_NAME} (or ORBIT_DEMO=1)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orbit.cli")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hash-password", help="print an argon2 hash line for .env")
    push = commands.add_parser("test-push", help="send a test push notification via ntfy")
    push.add_argument(
        "--action",
        action="store_true",
        help="add a Reject button for a throwaway Inbox item (tests the phone's round trip)",
    )
    copy_db = commands.add_parser(
        "copy-db", help="copy all data from another database into an empty, migrated one"
    )
    copy_db.add_argument("source", help="source database URL, e.g. sqlite:////import/orbit.db")
    copy_db.add_argument("--to", dest="target", help="target database URL (default: DATABASE_URL)")
    smoke = commands.add_parser(
        "smoke-test", help="read-only checks of a running instance (HTTPS, API, ICS feed)"
    )
    smoke.add_argument("--url", help="base URL to check (default: ORBIT_URL)")
    usage_cmd = commands.add_parser("usage", help="Claude calls and tokens per month")
    usage_cmd.add_argument(
        "--months", type=int, default=3, help="how many months back, this one included"
    )
    export_cmd = commands.add_parser("export", help="write all data as JSON")
    export_cmd.add_argument("-o", "--output", help="file to write (default: stdout)")
    seed = commands.add_parser(
        "seed-demo", help="fill an empty database with a fictional demo persona (no network)"
    )
    seed.add_argument(
        "--force", action="store_true", help="delete everything in the database first"
    )
    commands.add_parser(
        "prompts-snapshot",
        help="record the prompt text hashes and versions for the prompt-version test",
    )
    args = parser.parse_args(argv)
    if args.command == "hash-password":
        return _hash_password()
    if args.command == "test-push":
        return _test_push(args.action)
    if args.command == "copy-db":
        return _copy_db(args.source, args.target)
    if args.command == "smoke-test":
        return _smoke_test(args.url)
    if args.command == "usage":
        return _usage(max(1, args.months))
    if args.command == "export":
        return _export(args.output)
    if args.command == "seed-demo":
        return _seed_demo(args.force)
    if args.command == "prompts-snapshot":
        return _prompts_snapshot()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
