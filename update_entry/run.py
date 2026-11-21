#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "tzdata"]
# ///

"""Update one PoopJob entry, sending only the fields that belong to its kind.

The API's PATCH bodies differ per kind, and a field from the wrong kind is
rejected here with a message naming the fields the kind does accept. A
breastfeeding update replaces the whole segment list and recomputes the
session's startedAt/endedAt from it, because the API has no per-segment edit.

A diaper color is cleared by passing color 'none' (or null), which becomes
JSON null in the PATCH body.
"""

import json
import pathlib
import sys
from urllib.parse import quote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _poopjob
from _poopjob import ToolError

# Optional update fields the tool accepts, before kind-specific filtering.
FIELD_NAMES = (
    "type",
    "color",
    "occurred_at",
    "volume_ml",
    "contents",
    "size",
    "grams",
    "segments",
)

ALLOWED_FIELDS = {
    "diapers": {"type", "color", "occurred_at"},
    "breastfeeding": {"segments"},
    "bottles": {"occurred_at", "volume_ml", "contents"},
    "spit_ups": {"occurred_at", "size"},
    "weights": {"occurred_at", "grams"},
}


def _entry_id(params: dict) -> str:
    value = params.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ToolError("id is required.")
    return value.strip()


def build_body(kind: _poopjob.Kind, params: dict, config: _poopjob.Config):
    """Validate parameters and build the PATCH body for one kind."""
    unknown_params = set(params) - {"kind", "id"} - set(FIELD_NAMES)
    if unknown_params:
        raise ToolError(
            "Unknown parameter(s): " + ", ".join(sorted(unknown_params)) + "."
        )

    requested = {name for name in FIELD_NAMES if name in params}
    allowed = ALLOWED_FIELDS[kind.name]
    wrong = requested - allowed
    if wrong:
        raise ToolError(
            f"{', '.join(sorted(wrong))} is not a {kind.name} field. "
            f"{kind.name} supports: {', '.join(sorted(allowed))}."
        )
    if not requested:
        raise ToolError(
            "Nothing to update. Provide at least one of: "
            + ", ".join(sorted(allowed))
            + "."
        )

    body = {}
    if "type" in requested:
        body["type"] = _poopjob.require_choice(
            params["type"], "type", _poopjob.DIAPER_TYPES
        )
    if "color" in requested:
        color = params["color"]
        if color is None:
            body["color"] = None
        else:
            color = _poopjob.require_choice(
                color, "color", (*_poopjob.DIAPER_COLORS, "none")
            )
            body["color"] = None if color == "none" else color
    if "occurred_at" in requested:
        body["occurredAt"] = _poopjob.to_utc_iso(
            _poopjob.parse_time(params["occurred_at"], config)
        )
    if "volume_ml" in requested:
        body["volumeMl"] = _poopjob.require_integer(
            params["volume_ml"],
            "volume_ml",
            _poopjob.BOTTLE_VOLUME_MIN,
            _poopjob.BOTTLE_VOLUME_MAX,
        )
    if "contents" in requested:
        body["contents"] = _poopjob.require_choice(
            params["contents"], "contents", _poopjob.BOTTLE_CONTENTS
        )
    if "size" in requested:
        body["size"] = _poopjob.require_choice(
            params["size"], "size", _poopjob.SPIT_UP_SIZES
        )
    if "grams" in requested:
        body["grams"] = _poopjob.require_integer(
            params["grams"],
            "grams",
            _poopjob.WEIGHT_GRAMS_MIN,
            _poopjob.WEIGHT_GRAMS_MAX,
            hint=_poopjob.WEIGHT_GRAMS_HINT,
        )
    if "segments" in requested:
        segments, starts, ends = _poopjob.parse_segments(params["segments"], config)
        body["segments"] = segments
        body["startedAt"] = _poopjob.to_utc_iso(min(starts))
        body["endedAt"] = _poopjob.to_utc_iso(max(ends))

    return body


def update_entry(params: dict, config: _poopjob.Config, deadline: float) -> dict:
    kind = _poopjob.get_kind(params.get("kind"))
    entry_id = _entry_id(params)
    body = build_body(kind, params, config)

    payload = _poopjob.api_request(
        "PATCH",
        f"{kind.path}/{quote(entry_id, safe='')}",
        config=config,
        deadline=deadline,
        body=body,
    )
    if not isinstance(payload, dict):
        raise ToolError(f"The PoopJob API returned no updated {kind.name} entry.")

    return {
        "kind": kind.name,
        "entry": _poopjob.format_entry(kind.name, payload, config),
    }


def main() -> None:
    params = json.load(sys.stdin)
    config = _poopjob.load_config()
    deadline = _poopjob.api_deadline()
    json.dump(update_entry(params, config, deadline), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
