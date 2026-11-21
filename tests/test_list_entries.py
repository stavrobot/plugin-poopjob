#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "pytest", "tzdata"]
# ///
"""Mocked checks for the list_entries tool.

No test reads config.json or reaches the network: the tool is imported
directly, its config is constructed in memory, and requests.request is
replaced with a fake. The fake records every call so paging cursors and query
parameters can be asserted.

Run with: uv run --with pytest --with requests pytest
"""

import importlib.util
import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _poopjob  # noqa: E402
from _poopjob import ToolError  # noqa: E402


def _load_tool(name):
    path = ROOT / name / "run.py"
    spec = importlib.util.spec_from_file_location(f"poopjob_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


list_entries = _load_tool("list_entries")


class FakeResponse:
    def __init__(self, body=None, status_code=200):
        self.status_code = status_code
        self._body = body
        self.text = "" if body is None else json.dumps(body)
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeHTTP:
    """Serve responses from a handler and record every request."""

    def __init__(self):
        self.calls = []
        self.handler = lambda method, url, kwargs: FakeResponse(body=[])

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.handler(method, url, kwargs)


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(_poopjob.requests, "request", fake.request)
    return fake


@pytest.fixture
def config():
    return _poopjob.Config(
        api_key="pj_synthetic_key",
        timezone="Europe/Athens",
        tz=ZoneInfo("Europe/Athens"),
    )


@pytest.fixture
def fixed_now(monkeypatch):
    now = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(_poopjob, "now_utc", lambda: now)
    return now


def deadline():
    return _poopjob.api_deadline()


def at(hour, minute=0):
    return f"2026-01-12T{hour:02d}:{minute:02d}:00Z"


def diaper(entry_id, hour, minute=0):
    return {
        "id": entry_id,
        "type": "poop",
        "color": "mustard",
        "occurredAt": at(hour, minute),
        "createdAt": at(hour, minute),
    }


def spit_up(entry_id, hour):
    return {
        "id": entry_id,
        "occurredAt": at(hour),
        "size": "small",
        "createdAt": at(hour),
    }


def weight(entry_id, hour):
    return {
        "id": entry_id,
        "occurredAt": at(hour),
        "grams": 4200,
        "createdAt": at(hour),
    }


def page(items, has_more):
    return FakeResponse(body={"items": items, "hasMore": has_more})


def test_paged_kind_follows_has_more_and_stops_at_since(config, http, fixed_now):
    first = [diaper("d3", 11), diaper("d2", 10)]
    second = [diaper("d1", 9), diaper("d0", 8)]

    def handler(method, url, kwargs):
        before = kwargs["params"]["before"]
        if before == "2026-01-12T12:00:00Z":
            return page(first, True)
        assert before == "2026-01-12T10:00:00Z"
        return page(second, True)

    http.handler = handler

    result = list_entries.list_entries(
        {
            "kind": "diapers",
            "since": "2026-01-12T09:00:00Z",
        },
        config,
        deadline(),
    )

    # The 08:00 entry is older than since, so the page after it is never asked
    # for even though the API said hasMore. The 09:00 entry is exactly at
    # since and is included.
    assert len(http.calls) == 2
    assert [call["params"]["before"] for call in http.calls] == [
        "2026-01-12T12:00:00Z",
        "2026-01-12T10:00:00Z",
    ]
    assert [call["params"]["limit"] for call in http.calls] == [20, 18]
    assert result["kind"] == "diapers"
    assert [entry["id"] for entry in result["entries"]] == ["d3", "d2", "d1"]
    assert result["count"] == 3
    assert result["truncated"] is False
    assert "note" not in result
    # Timestamps are localized to the configured timezone.
    assert result["entries"][0]["occurred_at"] == "2026-01-12T13:00:00+02:00"


def test_paged_kind_stops_at_the_limit_and_says_it_is_truncated(
    config, http, fixed_now
):
    http.handler = lambda method, url, kwargs: page(
        [diaper("d5", 11), diaper("d4", 10), diaper("d3", 9)], True
    )

    result = list_entries.list_entries(
        {"kind": "diapers", "limit": 2}, config, deadline()
    )

    assert len(http.calls) == 1
    assert http.calls[0]["params"]["limit"] == 2
    assert [entry["id"] for entry in result["entries"]] == ["d5", "d4"]
    assert result["truncated"] is True
    assert "at most 2" in result["note"]
    # A third entry was visible in the page, so this is a definite match.
    assert "more match" in result["note"]
    assert "may match" not in result["note"]


def test_in_page_item_older_than_since_is_not_proof_of_more_matches(
    config, http, fixed_now
):
    http.handler = lambda method, url, kwargs: page(
        [diaper("d3", 11), diaper("d2", 10), diaper("d1", 8)], True
    )

    result = list_entries.list_entries(
        {
            "kind": "diapers",
            "limit": 2,
            "since": "2026-01-12T09:00:00Z",
        },
        config,
        deadline(),
    )

    # The next item is 08:00, below since, so nothing else in the window
    # matches and the result is complete.
    assert len(http.calls) == 1
    assert [entry["id"] for entry in result["entries"]] == ["d3", "d2"]
    assert result["truncated"] is False
    assert "note" not in result


def test_truncation_from_has_more_only_says_more_entries_may_match(
    config, http, fixed_now
):
    http.handler = lambda method, url, kwargs: page(
        [diaper("d2", 11), diaper("d1", 10)], True
    )

    result = list_entries.list_entries(
        {"kind": "diapers", "limit": 2}, config, deadline()
    )

    # The page was exactly full, so further matches are possible but unseen.
    assert len(http.calls) == 1
    assert result["truncated"] is True
    assert "more entries may match" in result["note"]
    assert "more match" not in result["note"]


def test_grouped_truncation_from_has_more_only_says_may_match(config, http, fixed_now):
    responses = {
        "/api/diapers": {"items": [diaper("d1", 11)], "hasMore": True},
        "/api/feedings": {"items": [], "hasMore": False},
        "/api/bottle-feedings": {"items": [], "hasMore": False},
        "/api/spit-ups": [],
        "/api/weights": [],
    }
    http.handler = lambda method, url, kwargs: FakeResponse(
        body=responses[url.removeprefix("https://my.poopjob.app")]
    )

    result = list_entries.list_entries({"limit": 1}, config, deadline())

    assert result["truncated"] is True
    assert "more entries may match" in result["note"]


def test_paged_kind_passes_until_as_the_before_bound(config, http, fixed_now):
    http.handler = lambda method, url, kwargs: page([], False)

    result = list_entries.list_entries(
        {"kind": "diapers", "until": "2026-01-12T10:00:00"}, config, deadline()
    )

    # 10:00 Athens is 08:00 UTC, and the tool must not use 'now' instead.
    assert http.calls[0]["params"]["before"] == "2026-01-12T08:00:00Z"
    assert result["entries"] == []
    assert result["truncated"] is False


def test_paged_cursor_that_does_not_advance_stops_instead_of_looping(
    config, http, fixed_now
):
    http.handler = lambda method, url, kwargs: page([diaper("d1", 11)], True)

    result = list_entries.list_entries({"kind": "diapers"}, config, deadline())

    assert len(http.calls) == 2
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "kind,entry,entry_id",
    [
        ("spit_ups", spit_up, "s1"),
        ("weights", weight, "w1"),
    ],
)
def test_non_paged_kind_is_fetched_whole_and_filtered_locally(
    config, http, fixed_now, kind, entry, entry_id
):
    http.handler = lambda method, url, kwargs: FakeResponse(
        body=[entry(entry_id, 11), entry("older", 8)]
    )

    result = list_entries.list_entries(
        {"kind": kind, "since": "2026-01-12T09:30:00Z"}, config, deadline()
    )

    # No cursor is sent; the endpoint has no paging support.
    assert http.calls[0]["params"] is None
    assert [item["id"] for item in result["entries"]] == [entry_id]
    assert result["truncated"] is False


def test_non_paged_local_limit_marks_truncation(config, http, fixed_now):
    http.handler = lambda method, url, kwargs: FakeResponse(
        body=[spit_up("s3", 11), spit_up("s2", 10), spit_up("s1", 9)]
    )

    result = list_entries.list_entries(
        {"kind": "spit_ups", "limit": 2}, config, deadline()
    )

    assert [item["id"] for item in result["entries"]] == ["s3", "s2"]
    assert result["truncated"] is True
    assert "at most 2" in result["note"]


def test_naive_api_timestamps_are_treated_as_utc(config, http, fixed_now):
    # 10:00 would fall outside the window if it were read as local Athens
    # time (08:00 UTC), so this proves the API timestamp is read as UTC.
    http.handler = lambda method, url, kwargs: FakeResponse(
        body=[
            {
                "id": "s1",
                "occurredAt": "2026-01-12T10:00:00",
                "size": "small",
                "createdAt": "2026-01-12T10:00:00",
            }
        ]
    )

    result = list_entries.list_entries(
        {"kind": "spit_ups", "since": "2026-01-12T09:00:00Z"}, config, deadline()
    )

    assert [item["id"] for item in result["entries"]] == ["s1"]


def test_request_that_fails_at_the_deadline_returns_partial(config, http, monkeypatch):
    real_now = time.monotonic()
    ticks = iter([real_now, real_now + 100])
    monkeypatch.setattr(
        list_entries, "time", types.SimpleNamespace(monotonic=lambda: next(ticks))
    )

    def timeout(*args, **kwargs):
        raise requests.exceptions.Timeout("slow")

    http.handler = timeout

    result = list_entries.list_entries({"kind": "diapers"}, config, real_now + 10)

    assert result["entries"] == []
    assert result["truncated"] is True
    assert "time budget" in result["note"]


def test_non_paged_until_is_exclusive_like_the_api_before_bound(
    config, http, fixed_now
):
    http.handler = lambda method, url, kwargs: FakeResponse(
        body=[spit_up("s2", 10), spit_up("s1", 9)]
    )

    result = list_entries.list_entries(
        {"kind": "spit_ups", "until": "2026-01-12T10:00:00Z"}, config, deadline()
    )

    # The 10:00 entry is exactly at until and is excluded, matching the API's
    # strict 'before' bound.
    assert [item["id"] for item in result["entries"]] == ["s1"]


def test_all_kinds_are_grouped_and_empty_kinds_are_kept(config, http, fixed_now):
    responses = {
        "/api/diapers": {"items": [diaper("d1", 11)], "hasMore": False},
        "/api/feedings": {"items": [], "hasMore": False},
        "/api/bottle-feedings": {"items": [], "hasMore": False},
        "/api/spit-ups": [spit_up("s1", 10)],
        "/api/weights": [],
    }
    http.handler = lambda method, url, kwargs: FakeResponse(
        body=responses[url.removeprefix("https://my.poopjob.app")]
    )

    result = list_entries.list_entries({"limit": 5}, config, deadline())

    assert set(result["kinds"]) == set(_poopjob.KIND_NAMES)
    assert result["count"] == 2
    assert result["truncated"] is False
    assert result["kinds"]["diapers"]["count"] == 1
    assert result["kinds"]["bottles"]["entries"] == []
    assert result["kinds"]["spit_ups"]["entries"][0]["id"] == "s1"


def test_expired_deadline_returns_a_truncated_result_without_a_request(config, http):
    result = list_entries.list_entries(
        {"kind": "diapers"}, config, time.monotonic() - 1
    )

    assert http.calls == []
    assert result["entries"] == []
    assert result["truncated"] is True
    assert "time budget" in result["note"]


def test_limit_is_clamped_to_200(config, http, fixed_now):
    http.handler = lambda method, url, kwargs: page([], False)

    list_entries.list_entries({"kind": "diapers", "limit": 500}, config, deadline())

    assert http.calls[0]["params"]["limit"] == 200


@pytest.mark.parametrize("value", [0, -3, "many", "1.5"])
def test_invalid_limit_is_rejected(config, http, value):
    with pytest.raises(ToolError) as exc:
        list_entries.list_entries(
            {"kind": "diapers", "limit": value}, config, deadline()
        )
    assert "limit" in str(exc.value)
    assert http.calls == []


def test_blank_kind_means_all_kinds(config, http, fixed_now):
    http.handler = lambda method, url, kwargs: FakeResponse(body=[])
    result = list_entries.list_entries({"kind": "  "}, config, deadline())
    assert "kinds" in result


def test_unknown_kind_is_rejected(config, http):
    with pytest.raises(ToolError) as exc:
        list_entries.list_entries({"kind": "nappies"}, config, deadline())
    assert "nappies" in str(exc.value)
    assert http.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
