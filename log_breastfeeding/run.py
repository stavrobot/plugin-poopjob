#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "tzdata"]
# ///

import json
import pathlib
import sys

# The shared root module holds config loading, time parsing, the HTTP helper
# and the entry formatter. It lives at the plugin root so every tool adopts it
# the same way.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _poopjob
from _poopjob import ToolError

KNOWN_PARAMS = {"segments"}


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    config = _poopjob.load_config()
    segments, starts, ends = _poopjob.parse_segments(params.get("segments"), config)

    body = {
        "startedAt": _poopjob.to_utc_iso(min(starts)),
        "endedAt": _poopjob.to_utc_iso(max(ends)),
        "segments": segments,
    }

    entry = _poopjob.api_request(
        "POST",
        _poopjob.KINDS["breastfeeding"].path,
        config=config,
        deadline=_poopjob.api_deadline(),
        body=body,
    )

    json.dump(_poopjob.format_entry("breastfeeding", entry, config), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
