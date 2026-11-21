#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "tzdata"]
# ///

import json
import pathlib
import sys

# The shared root module holds config loading, the HTTP helper and the entry
# formatter. It lives at the plugin root so every tool adopts it the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _poopjob
from _poopjob import ToolError

KNOWN_PARAMS = {"type", "color", "occurred_at"}


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    diaper_type = _poopjob.require_choice(
        params.get("type"), "type", _poopjob.DIAPER_TYPES
    )
    color = _poopjob.optional_choice(
        params.get("color"), "color", _poopjob.DIAPER_COLORS
    )
    if color is not None and diaper_type == "pee":
        raise ToolError(
            "A pee diaper cannot have a color. Leave color out, or use type "
            "'poop' or 'mix'."
        )

    config = _poopjob.load_config()

    occurred_at = _poopjob.optional_time_or_now(params.get("occurred_at"), config)

    body = {"type": diaper_type, "occurredAt": occurred_at}
    if color is not None:
        body["color"] = color

    entry = _poopjob.api_request(
        "POST",
        _poopjob.KINDS["diapers"].path,
        config=config,
        deadline=_poopjob.api_deadline(),
        body=body,
    )

    json.dump(_poopjob.format_entry("diapers", entry, config), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
