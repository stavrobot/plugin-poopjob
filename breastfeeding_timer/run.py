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

KNOWN_PARAMS = {"action", "side"}

ACTIONS = ("start", "switch", "pause", "resume", "end", "cancel", "status")

NO_ACTIVE_SESSION = "No breastfeeding session is active."

# A 409 means the action does not fit the current state. The API's message is
# surfaced by the shared helper, but it can be terse, so each action adds what
# the assistant should do next. This keeps a conflict actionable instead of
# leaving a bare status code.
CONFLICT_HINTS = {
    "start": (
        "A breastfeeding session is already active. Check it with action "
        "'status', then end or cancel it before starting another."
    ),
    "switch": "There is no active session, or it is paused. Check action 'status'.",
    "pause": "There is no active session to pause. Check action 'status'.",
    "resume": "There is no paused session to resume. Check action 'status'.",
    "end": "There is no active session to end. Check action 'status'.",
    "cancel": "There is no active session to cancel. Check action 'status'.",
}


def _get_active(config, deadline):
    """Fetch the active session, or None when there is not one."""
    return _poopjob.api_request(
        "GET",
        "/api/feedings/active",
        config=config,
        deadline=deadline,
    )


def main() -> None:
    params = json.load(sys.stdin)

    unknown = set(params.keys()) - KNOWN_PARAMS
    if unknown:
        raise ToolError(f"Unknown parameters: {', '.join(sorted(unknown))}")

    action = _poopjob.require_choice(params.get("action"), "action", ACTIONS)
    side = _poopjob.optional_choice(params.get("side"), "side", _poopjob.SIDES)

    if action == "start" and side is None:
        raise ToolError("action 'start' requires side: left or right.")
    if action not in ("start", "switch") and side is not None:
        raise ToolError(
            f"side is not used by action '{action}'. Pass it only with start or switch."
        )

    config = _poopjob.load_config()
    deadline = _poopjob.api_deadline()

    if action == "status":
        entry = _get_active(config, deadline)
        if entry is None:
            json.dump({"active": False, "message": NO_ACTIVE_SESSION}, sys.stdout)
        else:
            formatted = _poopjob.format_entry("breastfeeding", entry, config)
            json.dump({"active": True, **formatted}, sys.stdout)
        return

    body = None
    if action == "start":
        body = {"side": side}
    elif action == "switch":
        # Without a side the API toggles to the other one; it requires a JSON
        # body, so an empty object is sent rather than no body at all.
        body = {"side": side} if side is not None else {}

    try:
        entry = _poopjob.api_request(
            "POST",
            f"/api/feedings/{action}",
            config=config,
            deadline=deadline,
            body=body,
        )
    except ToolError as err:
        hint = CONFLICT_HINTS.get(action)
        if hint is not None and err.status == 409:
            raise ToolError(f"{err} {hint}") from err
        raise

    if action == "cancel":
        json.dump(
            {
                "cancelled": True,
                "message": "The active breastfeeding session was discarded.",
            },
            sys.stdout,
        )
        return

    json.dump(_poopjob.format_entry("breastfeeding", entry, config), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
