#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "tzdata"]
# ///

"""Delete one PoopJob entry.

The API refuses to delete the active breastfeeding session with HTTP 409, so
that case is rewritten into a message pointing at breastfeeding_timer's cancel
action rather than leaving a bare conflict.
"""

import json
import pathlib
import sys
from urllib.parse import quote

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _poopjob
from _poopjob import ToolError


def _entry_id(params: dict) -> str:
    value = params.get("id")
    if not isinstance(value, str) or not value.strip():
        raise ToolError("id is required.")
    return value.strip()


def delete_entry(params: dict, config: _poopjob.Config, deadline: float) -> dict:
    kind = _poopjob.get_kind(params.get("kind"))
    entry_id = _entry_id(params)

    try:
        _poopjob.api_request(
            "DELETE",
            f"{kind.path}/{quote(entry_id, safe='')}",
            config=config,
            deadline=deadline,
        )
    except ToolError as err:
        if kind.name == "breastfeeding" and err.status == 409:
            raise ToolError(
                f"{err} This is the active breastfeeding session, which cannot "
                "be deleted. Use breastfeeding_timer with action 'cancel' to "
                "cancel it instead."
            ) from None
        raise

    return {"deleted": True, "kind": kind.name, "id": entry_id}


def main() -> None:
    params = json.load(sys.stdin)
    config = _poopjob.load_config()
    deadline = _poopjob.api_deadline()
    json.dump(delete_entry(params, config, deadline), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
