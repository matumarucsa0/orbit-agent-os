"""Builds `Orbit Health.shortcut` (SPEC §2.36). Run on a Mac:

    python3 docs/shortcuts/build_orbit_health.py

Writes docs/shortcuts/Orbit Health.shortcut, signed with `shortcuts sign --mode anyone`, so it
imports on an iPhone. The Health filter structure was copied from a real shortcut exported from
the iPhone (Find Health Samples + Calculate Statistics); the other actions use Apple's standard
keys. Nothing secret is in the file: the Orbit address and the API token are asked at import.
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

OUT = Path(__file__).parent
OBJ = "￼"  # the placeholder character Shortcuts uses for an inserted variable


parser = argparse.ArgumentParser()
parser.add_argument(
    "--sleep-test",
    action="store_true",
    help="build 'Orbit Health Sleep Test': only sleep, unverified",
)
ARGS = parser.parse_args()


def uid() -> str:
    return str(uuid.uuid4()).upper()


def token(output_uuid: str, name: str) -> dict:
    return {"OutputUUID": output_uuid, "OutputName": name, "Type": "ActionOutput"}


def attachment(output_uuid: str, name: str) -> dict:
    return {"Value": token(output_uuid, name), "WFSerializationType": "WFTextTokenAttachment"}


def text_with(parts: list[str | tuple[str, str]]) -> dict:
    """A text value mixing literal text and variables: ("uuid", "Name") inserts a variable."""
    string, ranges = "", {}
    for part in parts:
        if isinstance(part, str):
            string += part
        else:
            ranges[f"{{{len(string)}, 1}}"] = token(*part)
            string += OBJ
    value: dict = {"string": string}
    if ranges:
        value["attachmentsByRange"] = ranges
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def action(identifier: str, params: dict) -> dict:
    return {"WFWorkflowActionIdentifier": identifier, "WFWorkflowActionParameters": params}


def health_filter(kind: str, days: int) -> dict:
    """Find Health Samples: Type is `kind`; Start Date is today (days = 0) or in the last
    `days` days. Careful: "in the last 1 day" means since the START OF YESTERDAY (two calendar
    days), not 24 hours (seen on a real phone). Operators: 4 is, 1001 in the last, 1002 is today
    (the numbers Cherri's open-source compiler uses)."""
    if days == 0:
        when: dict = {
            "Bounded": True,
            "Operator": 1002,
            "Property": "Start Date",
            "Removable": False,
            "Values": {},
        }
    else:
        when = {
            "Bounded": True,
            "Operator": 1001,
            "Property": "Start Date",
            "Removable": False,
            "Values": {"Number": str(days), "Unit": 16},
        }
    return {
        "Value": {
            "WFActionParameterFilterPrefix": 1,
            "WFActionParameterFilterTemplates": [
                {
                    "Bounded": True,
                    "Operator": 4,
                    "Property": "Type",
                    "Removable": False,
                    "Values": {
                        "Enumeration": {
                            "Value": kind,
                            "WFSerializationType": "WFStringSubstitutableState",
                        }
                    },
                },
                when,
            ],
            "WFContentPredicateBoundedDate": False,
        },
        "WFSerializationType": "WFContentPredicateTableTemplate",
    }


def dict_items(pairs: list[tuple[str, int, dict]]) -> dict:
    """A Shortcuts dictionary: (key, item type 0 text / 3 number, value)."""
    return {
        "Value": {
            "WFDictionaryFieldValueItems": [
                {
                    "WFItemType": kind,
                    "WFKey": {
                        "Value": {"string": key},
                        "WFSerializationType": "WFTextTokenString",
                    },
                    "WFValue": value,
                }
                for key, kind, value in pairs
            ]
        },
        "WFSerializationType": "WFDictionaryFieldValue",
    }


actions: list[dict] = []
import_questions: list[dict] = []

# 0 and 1: the Orbit address and the API token, asked once when the shortcut is imported.
url_id, token_id = uid(), uid()
actions.append(
    action(
        "is.workflow.actions.gettext",
        {"UUID": url_id, "WFTextActionText": "https://YOUR-ORBIT-HOST"},
    )
)
actions.append(
    action("is.workflow.actions.gettext", {"UUID": token_id, "WFTextActionText": "PASTE-API-TOKEN"})
)
import_questions.append(
    {
        "ActionIndex": 0,
        "Category": "Parameter",
        "DefaultValue": "https://",
        "ParameterKey": "WFTextActionText",
        "Text": "Your Orbit address, like https://orbit.example.com (no slash at the end)",
    }
)
import_questions.append(
    {
        "ActionIndex": 1,
        "Category": "Parameter",
        "DefaultValue": "",
        "ParameterKey": "WFTextActionText",
        "Text": "Your Orbit API token (ORBIT_API_TOKEN from the server .env)",
    }
)

# No date steps: Orbit uses today's date (Europe/Prague) when the request has no `day`. The shortcut
# runs in the morning, so the night just ended and the last 24 hours belong to today's row.

# (JSON key, Health type name in Shortcuts, statistic, days).
# Left out on purpose until they are verified on the phone:
#   ("active_kcal", "Active Energy", "Sum", 1): the watch may not write it to Health, and one empty
#       Find Health Samples step stops the whole shortcut ("No Samples Found").
#   ("sleep_minutes", "Sleep", "Sum", 1): sleep samples are labels (Asleep, Awake...), not numbers,
#       so Calculate Statistics fails ("couldn't convert from Text to Number"). It needs
#       Get Details of Health Samples (Duration) after a filter on the value; see SHORTCUTS.md.
METRICS = [
    ("steps", "Steps", "Sum", 0),
    ("resting_hr", "Resting Heart Rate", "Average", 0),
    ("avg_hr", "Heart Rate", "Average", 0),
    ("min_hr", "Heart Rate", "Minimum", 0),
    ("max_hr", "Heart Rate", "Maximum", 0),
    ("weight_kg", "Weight", "Average", 7),
]
stat_ids: dict[str, str] = {}
if ARGS.sleep_test:
    # EXPERIMENT. Sleep samples are labels, so Calculate Statistics cannot sum them; their
    # Duration can be summed. The "properties.<type>" getter pattern (WFInput +
    # WFContentItemPropertyName) is how Cherri's open-source action list defines every
    # "Get Details of ..." action; the Health identifier comes from a shortcut source viewer.
    # Not yet run on a phone. The unit of the sum (seconds or minutes) is learned from the
    # server's answer: Orbit refuses sleep_minutes above 1440.
    sample_id, props_id, stat_id = uid(), uid(), uid()
    actions.append(
        action(
            "is.workflow.actions.filter.health.quantity",
            {"UUID": sample_id, "WFContentItemFilter": health_filter("Sleep", 1)},
        )
    )
    actions.append(
        action(
            "is.workflow.actions.properties.health.quantity",
            {
                "UUID": props_id,
                "WFInput": attachment(sample_id, "Health Samples"),
                "WFContentItemPropertyName": "Duration",
            },
        )
    )
    actions.append(
        action(
            "is.workflow.actions.statistics",
            {
                "UUID": stat_id,
                "Input": attachment(props_id, "Details of Health Samples"),
                "WFStatisticsOperation": "Sum",
            },
        )
    )
    stat_ids["sleep_minutes"] = stat_id
    METRICS = [("sleep_minutes", "Sleep", "Sum", 1)]
else:
    for key, kind, operation, days in METRICS:
        sample_id, stat_id = uid(), uid()
        actions.append(
            action(
                "is.workflow.actions.filter.health.quantity",
                {"UUID": sample_id, "WFContentItemFilter": health_filter(kind, days)},
            )
        )
        actions.append(
            action(
                "is.workflow.actions.statistics",
                {
                    "UUID": stat_id,
                    "Input": attachment(sample_id, "Health Samples"),
                    "WFStatisticsOperation": operation,
                },
            )
        )
        stat_ids[key] = stat_id

body = [(key, 3, text_with([(stat_ids[key], "Statistics")])) for key, *_ in METRICS]

if not ARGS.sleep_test:
    # Sleep: four parallel text columns, one line per Health sleep sample (Start Date, End Date,
    # Duration, Value). Orbit picks the night and adds the stages (services/sleep_parse.py), because
    # Shortcuts cannot sum sleep by stage. Format verified from a real run of the sleep test.
    sleep_id = uid()
    actions.append(
        action(
            "is.workflow.actions.filter.health.quantity",
            {"UUID": sleep_id, "WFContentItemFilter": health_filter("Sleep", 1)},
        )
    )
    for key, prop in (
        ("sleep_starts", "Start Date"),
        ("sleep_ends", "End Date"),
        ("sleep_durations", "Duration"),
        ("sleep_values", "Value"),
    ):
        prop_id = uid()
        actions.append(
            action(
                "is.workflow.actions.properties.health.quantity",
                {
                    "UUID": prop_id,
                    "WFInput": attachment(sleep_id, "Health Samples"),
                    "WFContentItemPropertyName": prop,
                },
            )
        )
        body.append((key, 0, text_with([(prop_id, "Details of Health Samples")])))

post_id = uid()
actions.append(
    action(
        "is.workflow.actions.downloadurl",
        {
            "UUID": post_id,
            "WFURL": text_with([(url_id, "Text"), "/api/v1/health/daily"]),
            "WFHTTPMethod": "POST",
            "WFHTTPBodyType": "JSON",
            "WFHTTPHeaders": dict_items(
                [
                    ("Authorization", 0, text_with(["Bearer ", (token_id, "Text")])),
                    ("Content-Type", 0, text_with(["application/json"])),
                ]
            ),
            "WFJSONValues": dict_items(body),
            "ShowHeaders": True,
        },
    )
)

if ARGS.sleep_test:
    # Offline probe: no address, no token, no upload. It lists the sleep samples as text (one
    # column per property, same order) so the result can be pasted into a chat. Property names
    # are guesses from the Health sample fields; a wrong one shows as an error on that step.
    sample_id = uid()
    probe: list[dict] = [
        action(
            "is.workflow.actions.filter.health.quantity",
            {"UUID": sample_id, "WFContentItemFilter": health_filter("Sleep", 1)},
        )
    ]
    parts: list[str | tuple[str, str]] = ["SLEEP TEST (last 1 day)\n"]
    for prop in ("Start Date", "End Date", "Duration", "Value", "Source"):
        prop_id = uid()
        probe.append(
            action(
                "is.workflow.actions.properties.health.quantity",
                {
                    "UUID": prop_id,
                    "WFInput": attachment(sample_id, "Health Samples"),
                    "WFContentItemPropertyName": prop,
                },
            )
        )
        parts += [f"\n[{prop}]\n", (prop_id, "Details of Health Samples")]
    sum_prop_id, sum_id = uid(), uid()
    probe.append(
        action(
            "is.workflow.actions.properties.health.quantity",
            {
                "UUID": sum_prop_id,
                "WFInput": attachment(sample_id, "Health Samples"),
                "WFContentItemPropertyName": "Duration",
            },
        )
    )
    probe.append(
        action(
            "is.workflow.actions.statistics",
            {
                "UUID": sum_id,
                "Input": attachment(sum_prop_id, "Details of Health Samples"),
                "WFStatisticsOperation": "Sum",
            },
        )
    )
    parts += ["\n\n[Sum of Duration]\n", (sum_id, "Statistics")]
    # One Text action holds everything, so it can be copied in one go (and shown).
    text_id = uid()
    probe.append(
        action(
            "is.workflow.actions.gettext", {"UUID": text_id, "WFTextActionText": text_with(parts)}
        )
    )
    probe.append(
        action("is.workflow.actions.setclipboard", {"WFInput": attachment(text_id, "Text")})
    )
    probe.append(
        action(
            "is.workflow.actions.showresult",
            {
                "Text": text_with(
                    ["Copied to the clipboard. Paste it in the chat.\n\n", (text_id, "Text")]
                )
            },
        )
    )
    actions[:] = probe
    import_questions.clear()

workflow = {
    "WFWorkflowActions": actions,
    "WFWorkflowClientVersion": "5037.109",
    "WFWorkflowHasOutputFallback": False,
    "WFWorkflowHasShortcutInputVariables": False,
    "WFWorkflowIcon": {"WFWorkflowIconGlyphNumber": 61440, "WFWorkflowIconStartColor": -615917313},
    "WFWorkflowImportQuestions": import_questions,
    "WFWorkflowInputContentItemClasses": [],
    "WFWorkflowMinimumClientVersion": 900,
    "WFWorkflowMinimumClientVersionString": "900",
    "WFWorkflowOutputContentItemClasses": [],
    "WFWorkflowTypes": ["WFWorkflowTypeShowInSearch"],
}

signed = OUT / ("Orbit Health Sleep Test.shortcut" if ARGS.sleep_test else "Orbit Health.shortcut")
# `shortcuts sign` refuses to write into some folders: sign in a temp folder, then copy.
with tempfile.TemporaryDirectory() as tmp:
    raw, done = Path(tmp) / "in.shortcut", Path(tmp) / "out.shortcut"
    raw.write_bytes(plistlib.dumps(workflow, fmt=plistlib.FMT_BINARY))
    subprocess.run(
        ["shortcuts", "sign", "--mode", "anyone", "--input", str(raw), "--output", str(done)],
        check=True,
    )
    shutil.copyfile(done, signed)
print(f"wrote {signed} ({signed.stat().st_size} bytes)")
