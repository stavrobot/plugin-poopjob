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

KNOWN_PARAMS = {"volume_ml", "contents", "occurred_at"}


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    volume_ml = _poopjob.require_integer(
        params.get("volume_ml"),
        "volume_ml",
        _poopjob.BOTTLE_VOLUME_MIN,
        _poopjob.BOTTLE_VOLUME_MAX,
    )
    contents = _poopjob.require_choice(
        params.get("contents"), "contents", _poopjob.BOTTLE_CONTENTS
    )

    config = _poopjob.load_config()

    occurred_at = _poopjob.optional_time_or_now(params.get("occurred_at"), config)

    # The API requires occurredAt, so it is always sent.
    body = {
        "occurredAt": occurred_at,
        "volumeMl": volume_ml,
        "contents": contents,
    }

    entry = _poopjob.api_request(
        "POST",
        _poopjob.KINDS["bottles"].path,
        config=config,
        deadline=_poopjob.api_deadline(),
        body=body,
    )

    json.dump(_poopjob.format_entry("bottles", entry, config), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
