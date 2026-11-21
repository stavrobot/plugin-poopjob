#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "tzdata"]
# ///

"""List PoopJob entries, newest first, across one or all kinds.

The three paged kinds are read through the API's 'before' cursor: every
request passes a 'before' bound, because without one the API returns the whole
history in a bare array. The loop follows 'hasMore' using the last item's
timestamp until it passes 'since' or reaches the requested limit. Spit-ups and
weights have no cursor, so their list is fetched once and filtered locally.

Entries are newest first and 'since' is inclusive. 'until' is passed straight
through as the API's strict 'before' bound, so an entry exactly at 'until' is
excluded. If the shared deadline runs out mid-listing, the partial result is
returned with a note rather than being killed by the runner.
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import _poopjob
from _poopjob import ToolError

DEFAULT_LIMIT = 20
MAX_LIMIT = 200

# The API documents at most 200 items per 'before' page. Asking for more is
# pointless and the extra is silently ignored, so keep every request within it.
PAGE_SIZE = 200


def _limit(params: dict) -> int:
    """Read and bound the requested per-kind limit."""
    raw = params.get("limit")
    if raw is None:
        return DEFAULT_LIMIT
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"limit must be a whole number, got '{raw}'.") from None
    if limit < 1:
        raise ToolError("limit must be at least 1.")
    return min(limit, MAX_LIMIT)


def _optional_time(params: dict, name: str, config: _poopjob.Config):
    """Parse an optional since/until parameter to an aware UTC datetime."""
    value = params.get(name)
    if value is None:
        return None
    return _poopjob.parse_time(value, config)


def _entry_time(kind: _poopjob.Kind, entry):
    """Read an entry's ordering timestamp, failing clearly if it is missing."""
    if not isinstance(entry, dict):
        raise ToolError(f"The PoopJob API returned an unexpected {kind.name} entry.")
    value = entry.get(kind.time_field)
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            f"The PoopJob API returned a {kind.name} entry without '{kind.time_field}'."
        )
    return _poopjob.parse_api_time(value)


def _page(payload, kind: _poopjob.Kind):
    """Split a paged response into (items, has_more).

    With 'before' the API returns {items, hasMore}, but a bare array is still
    accepted: treating it as a final page is correct, just less efficient.
    """
    if isinstance(payload, dict):
        items = payload.get("items")
        if not isinstance(items, list):
            raise ToolError(
                f"The PoopJob API returned {kind.name} without an 'items' list."
            )
        return items, bool(payload.get("hasMore"))
    if isinstance(payload, list):
        return payload, False
    raise ToolError(f"The PoopJob API returned an unexpected {kind.name} list.")


def _fetch_paged(kind, config, deadline, since, until, limit, now):
    """Read a kind that supports the 'before' cursor.

    Returns (entries, truncated, deadline_hit, uncertain). Entries are raw API
    objects, newest first. 'truncated' means the window was cut short.
    'uncertain' means the cut is known only from the API's hasMore, so more
    entries may match but the current pages did not show one.
    """
    before_time = until if until is not None else now
    if since is not None and before_time <= since:
        return [], False, False, False

    before = _poopjob.to_utc_iso(before_time)
    entries = []

    while True:
        if time.monotonic() >= deadline:
            return entries, True, True, False

        want = min(limit - len(entries), PAGE_SIZE)
        try:
            payload = _poopjob.api_request(
                "GET",
                kind.path,
                config=config,
                deadline=deadline,
                params={"limit": want, "before": before},
            )
        except ToolError:
            # A request whose per-read timeout is the remaining budget can
            # fail right at the deadline. Report what was collected rather
            # than a budget error that discards it.
            if time.monotonic() >= deadline:
                return entries, True, True, False
            raise
        items, has_more = _page(payload, kind)
        if not items:
            return entries, False, False, False

        last_time = None
        for index, item in enumerate(items):
            last_time = _entry_time(kind, item)
            if since is not None and last_time < since:
                return entries, False, False, False
            entries.append(item)
            if len(entries) >= limit:
                if index < len(items) - 1:
                    # The next item is a known match only if it is still
                    # inside the window; anything older ends the window.
                    next_time = _entry_time(kind, items[index + 1])
                    if since is not None and next_time < since:
                        return entries, False, False, False
                    return entries, True, False, False
                # Otherwise only the API's hasMore suggests a match, so say
                # "may".
                return entries, has_more, False, has_more

        if not has_more:
            return entries, False, False, False

        next_before = _poopjob.to_utc_iso(last_time)
        if next_before == before:
            # The API's cursor did not advance; stop instead of looping.
            return entries, True, False, True
        before = next_before


def _fetch_plain(kind, config, deadline, since, until, limit):
    """Read a kind with no paging, filtering and limiting locally.

    The whole list is fetched because the endpoint offers no cursor: asking
    for a bounded page could silently miss older entries that still fall
    inside 'since'. Only the newest 'limit' matches are returned.
    """
    if time.monotonic() >= deadline:
        return [], True, True, False

    try:
        payload = _poopjob.api_request(
            "GET", kind.path, config=config, deadline=deadline
        )
    except ToolError:
        if time.monotonic() >= deadline:
            return [], True, True, False
        raise
    if not isinstance(payload, list):
        raise ToolError(f"The PoopJob API returned an unexpected {kind.name} list.")

    matches = []
    for entry in payload:
        occurred = _entry_time(kind, entry)
        if since is not None and occurred < since:
            continue
        if until is not None and occurred >= until:
            continue
        matches.append(entry)

    return matches[:limit], len(matches) > limit, False, False


def _fetch(kind, config, deadline, since, until, limit, now):
    if kind.paged:
        return _fetch_paged(kind, config, deadline, since, until, limit, now)
    return _fetch_plain(kind, config, deadline, since, until, limit)


def _group(kind, entries, truncated, config):
    formatted = [_poopjob.format_entry(kind.name, entry, config) for entry in entries]
    return {
        "entries": formatted,
        "count": len(formatted),
        "truncated": truncated,
    }


def _deadline_note() -> str:
    return (
        "Results are incomplete: the PoopJob API time budget ran out before "
        "everything was listed. Narrow the time range or retry."
    )


def _limit_note(limit: int, grouped: bool, *, uncertain: bool) -> str:
    more = "more entries may match" if uncertain else "more match"
    if grouped:
        return (
            f"Showing at most {limit} entries per kind; {more}. Raise "
            "limit (maximum 200) or narrow the time range."
        )
    return (
        f"Showing at most {limit} entries; {more}. Raise limit "
        "(maximum 200) or narrow the time range."
    )


def list_entries(params: dict, config: _poopjob.Config, deadline: float) -> dict:
    """List entries for the requested kind, or all kinds grouped by kind."""
    limit = _limit(params)
    since = _optional_time(params, "since", config)
    until = _optional_time(params, "until", config)
    now = _poopjob.now_utc()

    raw_kind = params.get("kind")
    if raw_kind is not None and str(raw_kind).strip():
        kind = _poopjob.get_kind(raw_kind)
        entries, truncated, deadline_hit, uncertain = _fetch(
            kind, config, deadline, since, until, limit, now
        )
        result = {"kind": kind.name, **_group(kind, entries, truncated, config)}
        if deadline_hit:
            result["note"] = _deadline_note()
        elif truncated:
            result["note"] = _limit_note(limit, grouped=False, uncertain=uncertain)
        return result

    groups = {}
    total = 0
    any_truncated = False
    any_uncertain = False
    any_definite = False
    deadline_hit = False
    for kind in _poopjob.KINDS.values():
        entries, truncated, hit, uncertain = _fetch(
            kind, config, deadline, since, until, limit, now
        )
        group = _group(kind, entries, truncated, config)
        groups[kind.name] = group
        total += group["count"]
        any_truncated = any_truncated or truncated
        any_uncertain = any_uncertain or uncertain
        any_definite = any_definite or (truncated and not uncertain)
        if hit:
            deadline_hit = True
            break

    result = {
        "kinds": groups,
        "count": total,
        "truncated": any_truncated or deadline_hit,
    }
    if deadline_hit:
        result["note"] = _deadline_note()
    elif any_truncated:
        result["note"] = _limit_note(
            limit, grouped=True, uncertain=any_uncertain and not any_definite
        )
    return result


def main() -> None:
    params = json.load(sys.stdin)
    config = _poopjob.load_config()
    deadline = _poopjob.api_deadline()
    json.dump(list_entries(params, config, deadline), sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except ToolError as err:
        print(str(err), file=sys.stderr)
        sys.exit(1)
