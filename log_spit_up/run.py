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

KNOWN_PARAMS = {"size", "occurred_at"}


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    size = _poopjob.require_choice(params.get("size"), "size", _poopjob.SPIT_UP_SIZES)

    config = _poopjob.load_config()

    occurred_at = _poopjob.optional_time_or_now(params.get("occurred_at"), config)

    # The API requires occurredAt, so it is always sent.
    body = {"occurredAt": occurred_at, "size": size}

    entry = _poopjob.api_request(
        "POST",
        _poopjob.KINDS["spit_ups"].path,
        config=config,
        deadline=_poopjob.api_deadline(),
        body=body,
    )

    json.dump(_poopjob.format_entry("spit_ups", entry, config), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
