#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "pytest"]
# ///
"""Tool-level checks for the write tools and the breastfeeding timer.

Each tool is imported directly and driven through main(), with config loading
and the HTTP call replaced by fakes. The plugin root is redirected to a
temporary directory in every test, so these never read or write the real
config.json and never touch the live API.

Run with: uv run --with pytest --with requests pytest
"""

import importlib.util
import io
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _poopjob  # noqa: E402
from _poopjob import ToolError  # noqa: E402

CONFIG = _poopjob.Config(
    api_key="pj_synthetic_key",
    timezone="Europe/Athens",
    tz=ZoneInfo("Europe/Athens"),
)

FIXED_NOW = datetime(2026, 1, 12, 9, 0, tzinfo=timezone.utc)


def load_tool(name):
    """Import a tool's run.py under a unique module name."""
    path = ROOT / name / "run.py"
    spec = importlib.util.spec_from_file_location(f"poopjob_tool_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeApi:
    """Stand-in for _poopjob.api_request that records each call."""

    def __init__(self):
        self.calls = []
        self.result = {}
        self.error = None

    def __call__(self, method, path, *, config, deadline, body=None, params=None):
        self.calls.append(
            {
                "method": method,
                "path": path,
                "config": config,
                "deadline": deadline,
                "body": body,
                "params": params,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    """Redirect config.json reads away from the repository in every test."""
    monkeypatch.setattr(_poopjob, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def api(monkeypatch):
    fake = FakeApi()
    monkeypatch.setattr(_poopjob, "api_request", fake)
    monkeypatch.setattr(_poopjob, "load_config", lambda: CONFIG)
    monkeypatch.setattr(_poopjob, "now_utc", lambda: FIXED_NOW)
    return fake


def call_tool(monkeypatch, name, params):
    """Run a tool's main() with JSON on stdin and return its JSON output."""
    module = load_tool(name)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(params)))
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    module.main()
    return json.loads(sys.stdout.getvalue())


def diaper_entry(**overrides):
    entry = {
        "id": "d1",
        "type": "poop",
        "color": "mustard",
        "occurredAt": "2026-01-12T08:30:00Z",
        "createdAt": "2026-01-12T08:31:00Z",
    }
    entry.update(overrides)
    return entry


def bottle_entry(**overrides):
    entry = {
        "id": "b1",
        "occurredAt": "2026-01-12T08:30:00Z",
        "volumeMl": 90,
        "contents": "formula",
        "createdAt": "2026-01-12T08:31:00Z",
    }
    entry.update(overrides)
    return entry


def spit_up_entry(**overrides):
    entry = {
        "id": "s1",
        "occurredAt": "2026-01-12T08:30:00Z",
        "size": "small",
        "createdAt": "2026-01-12T08:31:00Z",
    }
    entry.update(overrides)
    return entry


def weight_entry(**overrides):
    entry = {
        "id": "w1",
        "occurredAt": "2026-01-12T08:30:00Z",
        "grams": 4200,
        "createdAt": "2026-01-12T08:31:00Z",
    }
    entry.update(overrides)
    return entry


def feeding_entry(**overrides):
    entry = {
        "id": "f1",
        "startedAt": "2026-01-12T06:00:00Z",
        "endedAt": "2026-01-12T06:20:00Z",
        "segments": [
            {
                "id": "s1",
                "side": "left",
                "startedAt": "2026-01-12T06:00:00Z",
                "endedAt": "2026-01-12T06:05:00Z",
            },
            {
                "id": "s2",
                "side": "right",
                "startedAt": "2026-01-12T06:10:00Z",
                "endedAt": "2026-01-12T06:20:00Z",
            },
        ],
        "createdAt": "2026-01-12T06:21:00Z",
    }
    entry.update(overrides)
    return entry


# ---------------------------------------------------------------------------
# log_diaper
# ---------------------------------------------------------------------------
def test_log_diaper_sends_valid_enum_values_and_formats_the_entry(api, monkeypatch):
    api.result = diaper_entry()
    output = call_tool(
        monkeypatch,
        "log_diaper",
        {
            "type": "Poop",
            "color": "Mustard",
            "occurred_at": "2026-01-12T10:30:00+02:00",
        },
    )

    call = api.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/diapers"
    assert call["body"] == {
        "type": "poop",
        "color": "mustard",
        "occurredAt": "2026-01-12T08:30:00Z",
    }
    assert output == {
        "id": "d1",
        "type": "poop",
        "color": "mustard",
        "occurred_at": "2026-01-12T10:30:00+02:00",
    }


def test_log_diaper_defaults_to_now_and_omits_an_absent_color(api, monkeypatch):
    api.result = diaper_entry(color=None)
    call_tool(monkeypatch, "log_diaper", {"type": "pee"})

    assert api.calls[0]["body"] == {
        "type": "pee",
        "occurredAt": "2026-01-12T09:00:00Z",
    }


def test_log_diaper_rejects_color_with_pee_before_calling_the_api(api, monkeypatch):
    with pytest.raises(ToolError) as exc:
        call_tool(monkeypatch, "log_diaper", {"type": "pee", "color": "mustard"})
    assert "pee diaper cannot have a color" in str(exc.value)
    assert api.calls == []


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"type": "nappy"},
        {"type": "poop", "color": "purple"},
        {"type": "poop", "colour": "tan"},
    ],
)
def test_log_diaper_rejects_invalid_input(api, monkeypatch, params):
    with pytest.raises(ToolError):
        call_tool(monkeypatch, "log_diaper", params)
    assert api.calls == []


# ---------------------------------------------------------------------------
# log_bottle
# ---------------------------------------------------------------------------
def test_log_bottle_always_sends_occurred_at_and_formats_the_entry(api, monkeypatch):
    api.result = bottle_entry()
    output = call_tool(
        monkeypatch, "log_bottle", {"volume_ml": 90, "contents": "formula"}
    )

    assert api.calls[0]["path"] == "/api/bottle-feedings"
    assert api.calls[0]["body"] == {
        "occurredAt": "2026-01-12T09:00:00Z",
        "volumeMl": 90,
        "contents": "formula",
    }
    assert output == {
        "id": "b1",
        "occurred_at": "2026-01-12T10:30:00+02:00",
        "volume_ml": 90,
        "contents": "formula",
    }


def test_log_bottle_sends_a_supplied_time(api, monkeypatch):
    api.result = bottle_entry()
    call_tool(
        monkeypatch,
        "log_bottle",
        {
            "volume_ml": 120,
            "contents": "breast_milk",
            "occurred_at": "2026-01-12T10:30:00",
        },
    )
    # A time without an offset is read in Europe/Athens (UTC+2 in January).
    assert api.calls[0]["body"]["occurredAt"] == "2026-01-12T08:30:00Z"


@pytest.mark.parametrize(
    "params",
    [
        {"contents": "formula"},
        {"volume_ml": 0, "contents": "formula"},
        {"volume_ml": 501, "contents": "formula"},
        {"volume_ml": "90", "contents": "formula"},
        {"volume_ml": True, "contents": "formula"},
        {"volume_ml": 90, "contents": "milk"},
        {"volume_ml": 90, "contents": "formula", "note": "x"},
    ],
)
def test_log_bottle_rejects_invalid_input(api, monkeypatch, params):
    with pytest.raises(ToolError):
        call_tool(monkeypatch, "log_bottle", params)
    assert api.calls == []


# ---------------------------------------------------------------------------
# log_spit_up
# ---------------------------------------------------------------------------
def test_log_spit_up_always_sends_occurred_at(api, monkeypatch):
    api.result = spit_up_entry()
    output = call_tool(monkeypatch, "log_spit_up", {"size": "Small"})

    assert api.calls[0]["path"] == "/api/spit-ups"
    assert api.calls[0]["body"] == {
        "occurredAt": "2026-01-12T09:00:00Z",
        "size": "small",
    }
    assert output == {
        "id": "s1",
        "occurred_at": "2026-01-12T10:30:00+02:00",
        "size": "small",
    }


@pytest.mark.parametrize("params", [{}, {"size": "huge"}])
def test_log_spit_up_rejects_invalid_input(api, monkeypatch, params):
    with pytest.raises(ToolError):
        call_tool(monkeypatch, "log_spit_up", params)
    assert api.calls == []


# ---------------------------------------------------------------------------
# log_weight
# ---------------------------------------------------------------------------
def test_log_weight_always_sends_occurred_at(api, monkeypatch):
    api.result = weight_entry()
    output = call_tool(monkeypatch, "log_weight", {"grams": 4200})

    assert api.calls[0]["path"] == "/api/weights"
    assert api.calls[0]["body"] == {
        "occurredAt": "2026-01-12T09:00:00Z",
        "grams": 4200,
    }
    assert output == {
        "id": "w1",
        "occurred_at": "2026-01-12T10:30:00+02:00",
        "grams": 4200,
    }


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"grams": 299},
        {"grams": 50001},
        {"grams": 4.2},
        {"grams": True},
        {"grams": "4200"},
    ],
)
def test_log_weight_rejects_invalid_input(api, monkeypatch, params):
    with pytest.raises(ToolError) as exc:
        call_tool(monkeypatch, "log_weight", params)
    assert api.calls == []
    assert "grams" in str(exc.value)


# ---------------------------------------------------------------------------
# log_breastfeeding
# ---------------------------------------------------------------------------
def test_log_breastfeeding_derives_session_bounds_from_all_segments(api, monkeypatch):
    api.result = feeding_entry()
    output = call_tool(
        monkeypatch,
        "log_breastfeeding",
        {
            "segments": [
                {
                    "side": "right",
                    "started_at": "2026-01-12T08:10:00+02:00",
                    "ended_at": "2026-01-12T08:20:00+02:00",
                },
                {
                    "side": "left",
                    "started_at": "2026-01-12T08:00:00+02:00",
                    "ended_at": "2026-01-12T08:05:00+02:00",
                },
            ]
        },
    )

    body = api.calls[0]["body"]
    assert api.calls[0]["path"] == "/api/feedings"
    assert body["startedAt"] == "2026-01-12T06:00:00Z"
    assert body["endedAt"] == "2026-01-12T06:20:00Z"
    assert body["segments"] == [
        {
            "side": "right",
            "startedAt": "2026-01-12T06:10:00Z",
            "endedAt": "2026-01-12T06:20:00Z",
        },
        {
            "side": "left",
            "startedAt": "2026-01-12T06:00:00Z",
            "endedAt": "2026-01-12T06:05:00Z",
        },
    ]
    assert output["left_minutes"] == 5.0
    assert output["right_minutes"] == 10.0
    assert output["total_minutes"] == 15.0


@pytest.mark.parametrize(
    "segments,expected",
    [
        ([], "At least one segment"),
        ("left", "must be an array"),
        (
            [{"side": "left", "started_at": "2026-01-12T08:00:00"}],
            "ended_at is required",
        ),
        (
            [
                {
                    "side": "middle",
                    "started_at": "2026-01-12T08:00:00+02:00",
                    "ended_at": "2026-01-12T08:05:00+02:00",
                }
            ],
            "Use left or right",
        ),
        (
            [
                {
                    "started_at": "2026-01-12T08:00:00+02:00",
                    "ended_at": "2026-01-12T08:05:00+02:00",
                }
            ],
            "side is required",
        ),
        (
            [
                {
                    "side": "left",
                    "started_at": "2026-01-12T08:05:00+02:00",
                    "ended_at": "2026-01-12T08:05:00+02:00",
                }
            ],
            "must end after it starts",
        ),
        (
            [
                {
                    "side": "left",
                    "started_at": "2026-01-12T08:05:00+02:00",
                    "ended_at": "2026-01-12T08:00:00+02:00",
                }
            ],
            "must end after it starts",
        ),
        (
            [
                {
                    "side": "left",
                    "started_at": "yesterday",
                    "ended_at": "2026-01-12T08:00:00+02:00",
                }
            ],
            "segments[0].started_at",
        ),
        (
            [
                {
                    "side": "left",
                    "started_at": "2026-01-12T08:00:00+02:00",
                    "ended_at": "2026-01-12T08:05:00+02:00",
                    "extra": 1,
                }
            ],
            "unknown fields",
        ),
    ],
)
def test_log_breastfeeding_rejects_invalid_segments(
    api, monkeypatch, segments, expected
):
    with pytest.raises(ToolError) as exc:
        call_tool(monkeypatch, "log_breastfeeding", {"segments": segments})
    assert expected in str(exc.value)
    assert api.calls == []


# ---------------------------------------------------------------------------
# breastfeeding_timer
# ---------------------------------------------------------------------------
def test_timer_start_requires_side(api, monkeypatch):
    with pytest.raises(ToolError) as exc:
        call_tool(monkeypatch, "breastfeeding_timer", {"action": "start"})
    assert "requires side" in str(exc.value)
    assert api.calls == []


def test_timer_start_sends_the_side(api, monkeypatch):
    api.result = feeding_entry(endedAt=None, segments=[])
    call_tool(monkeypatch, "breastfeeding_timer", {"action": "start", "side": "Right"})

    assert api.calls[0]["method"] == "POST"
    assert api.calls[0]["path"] == "/api/feedings/start"
    assert api.calls[0]["body"] == {"side": "right"}


def test_timer_switch_without_side_sends_an_empty_body(api, monkeypatch):
    api.result = feeding_entry(endedAt=None, segments=[])
    call_tool(monkeypatch, "breastfeeding_timer", {"action": "switch"})

    assert api.calls[0]["path"] == "/api/feedings/switch"
    assert api.calls[0]["body"] == {}


def test_timer_switch_with_side_sends_it(api, monkeypatch):
    api.result = feeding_entry(endedAt=None, segments=[])
    call_tool(monkeypatch, "breastfeeding_timer", {"action": "switch", "side": "left"})

    assert api.calls[0]["path"] == "/api/feedings/switch"
    assert api.calls[0]["body"] == {"side": "left"}


@pytest.mark.parametrize("action", ["pause", "resume", "end"])
def test_timer_simple_actions_post_with_no_body(api, monkeypatch, action):
    api.result = feeding_entry(
        endedAt=None if action == "pause" else "2026-01-12T06:20:00Z"
    )
    call_tool(monkeypatch, "breastfeeding_timer", {"action": action})

    assert api.calls[0]["method"] == "POST"
    assert api.calls[0]["path"] == f"/api/feedings/{action}"
    assert api.calls[0]["body"] is None


def test_timer_cancel_returns_a_confirmation(api, monkeypatch):
    api.result = {"ok": True}
    output = call_tool(monkeypatch, "breastfeeding_timer", {"action": "cancel"})

    assert api.calls[0]["path"] == "/api/feedings/cancel"
    assert output["cancelled"] is True
    assert "discarded" in output["message"]


def test_timer_status_without_an_active_session(api, monkeypatch):
    api.result = None
    output = call_tool(monkeypatch, "breastfeeding_timer", {"action": "status"})

    assert api.calls[0]["method"] == "GET"
    assert api.calls[0]["path"] == "/api/feedings/active"
    assert output == {"active": False, "message": "No breastfeeding session is active."}


def test_timer_status_with_an_active_session_formats_it(api, monkeypatch):
    api.result = feeding_entry(endedAt=None)
    output = call_tool(monkeypatch, "breastfeeding_timer", {"action": "status"})

    assert output["active"] is True
    assert output["id"] == "f1"
    assert output["ended_at"] is None
    assert output["total_minutes"] == 15.0


def test_timer_start_conflict_explains_what_to_do(api, monkeypatch):
    api.error = ToolError(
        "The PoopJob API returned HTTP 409 (conflict): A session is already running.",
        status=409,
    )
    with pytest.raises(ToolError) as exc:
        call_tool(
            monkeypatch, "breastfeeding_timer", {"action": "start", "side": "left"}
        )
    message = str(exc.value)
    assert "session is already running" in message
    assert "already active" in message
    assert "status" in message


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"action": "dance"},
        {"action": "pause", "side": "left"},
        {"action": "status", "side": "left"},
        {"action": "start", "side": "middle"},
        {"action": "start", "side": "left", "extra": 1},
    ],
)
def test_timer_rejects_invalid_input(api, monkeypatch, params):
    with pytest.raises(ToolError):
        call_tool(monkeypatch, "breastfeeding_timer", params)
    assert api.calls == []


def test_timer_side_uses_the_shared_validation_messages(api, monkeypatch):
    with pytest.raises(ToolError) as exc:
        call_tool(
            monkeypatch, "breastfeeding_timer", {"action": "start", "side": "middle"}
        )
    assert str(exc.value) == "Unknown side 'middle'. Valid values: left, right."

    with pytest.raises(ToolError) as exc:
        call_tool(monkeypatch, "breastfeeding_timer", {"action": "start", "side": 1})
    assert str(exc.value) == "'side' must be a string. Valid values: left, right."

    assert api.calls == []


def test_non_conflict_api_errors_pass_through(api, monkeypatch):
    api.error = ToolError("The PoopJob API returned HTTP 500: boom")
    with pytest.raises(ToolError) as exc:
        call_tool(monkeypatch, "breastfeeding_timer", {"action": "pause"})
    assert str(exc.value) == "The PoopJob API returned HTTP 500: boom"


def test_a_status_less_error_that_mentions_409_is_not_treated_as_a_conflict(
    api, monkeypatch
):
    api.error = ToolError("The PoopJob API returned HTTP 409: not from the API")
    with pytest.raises(ToolError) as exc:
        call_tool(
            monkeypatch, "breastfeeding_timer", {"action": "start", "side": "left"}
        )
    assert str(exc.value) == "The PoopJob API returned HTTP 409: not from the API"


# ---------------------------------------------------------------------------
# The error contract: ToolError goes to stderr with a non-zero exit.
# ---------------------------------------------------------------------------
def test_validation_error_prints_to_stderr_and_exits_non_zero():
    result = subprocess.run(
        [sys.executable, str(ROOT / "log_bottle" / "run.py")],
        input=json.dumps({"volume_ml": 0, "contents": "formula"}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "volume_ml" in result.stderr
    assert result.stdout == ""


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
