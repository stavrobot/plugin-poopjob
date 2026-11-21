#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "pytest"]
# ///
"""Mocked checks for _poopjob.py.

Every network call is replaced with a fake, and the plugin root is redirected
to a temporary directory in every test, so these never read or write the real
config.json and never touch the live API.

Run with: uv run --with pytest --with requests pytest
"""

import json
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import _poopjob  # noqa: E402
from _poopjob import ToolError  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    """Redirect config.json reads away from the repository in every test."""
    monkeypatch.setattr(_poopjob, "ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def config():
    return _poopjob.Config(
        api_key="pj_synthetic_key",
        timezone="Europe/Athens",
        tz=ZoneInfo("Europe/Athens"),
    )


class FakeResponse:
    def __init__(self, status_code=200, body=None, text=None, headers=None):
        self.status_code = status_code
        self._body = body
        if text is not None:
            self.text = text
        elif body is not None:
            self.text = json.dumps(body)
        else:
            self.text = ""
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class Recorder:
    def __init__(self):
        self.calls = []
        self.response = FakeResponse(body={})


@pytest.fixture
def http(monkeypatch):
    """Patch requests.request, returning a recorder and serving its response."""
    recorder = Recorder()

    def fake_request(method, url, **kwargs):
        recorder.calls.append({"method": method, "url": url, **kwargs})
        if isinstance(recorder.response, Exception):
            raise recorder.response
        return recorder.response

    monkeypatch.setattr(_poopjob.requests, "request", fake_request)
    return recorder


def call_api(
    config, http, *, method="GET", path="/api/diapers", body=None, params=None
):
    return _poopjob.api_request(
        method,
        path,
        config=config,
        deadline=_poopjob.api_deadline(),
        body=body,
        params=params,
    )


# ---------------------------------------------------------------------------
# Time parsing and output localization.
# ---------------------------------------------------------------------------
def test_parse_time_with_offset_converts_to_utc(config):
    parsed = _poopjob.parse_time("2026-01-12T10:30:00+02:00", config)
    assert parsed == datetime(2026, 1, 12, 8, 30, tzinfo=timezone.utc)
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)


def test_parse_time_naive_is_interpreted_in_the_config_timezone(config):
    parsed = _poopjob.parse_time("2026-01-12T10:30:00", config)
    assert parsed == datetime(2026, 1, 12, 8, 30, tzinfo=timezone.utc)


def test_parse_time_naive_uses_the_summer_offset_too(config):
    # Europe/Athens is UTC+3 in July, so a fixed-offset implementation fails.
    parsed = _poopjob.parse_time("2026-07-01T10:30:00", config)
    assert parsed == datetime(2026, 7, 1, 7, 30, tzinfo=timezone.utc)


def test_parse_time_rejects_a_spring_forward_gap(config):
    # Europe/Athens skips 03:00-03:59 on 2026-03-29, so this wall time never
    # happened and must not be guessed.
    with pytest.raises(ToolError) as exc:
        _poopjob.parse_time("2026-03-29T03:30:00", config)
    message = str(exc.value)
    assert "does not exist" in message
    assert "explicit UTC offset" in message


def test_parse_time_rejects_a_fall_back_repeat(config):
    # Europe/Athens repeats 03:00-03:59 on 2026-10-25, so this wall time
    # happened twice and must not be guessed.
    with pytest.raises(ToolError) as exc:
        _poopjob.parse_time("2026-10-25T03:30:00", config)
    message = str(exc.value)
    assert "ambiguous" in message
    assert "explicit UTC offset" in message


def test_parse_time_accepts_an_offset_on_a_dst_edge(config):
    assert _poopjob.parse_time("2026-10-25T03:30:00+02:00", config) == datetime(
        2026, 10, 25, 1, 30, tzinfo=timezone.utc
    )


def test_parse_time_accepts_a_z_suffix(config):
    parsed = _poopjob.parse_time("2026-01-12T08:30:00Z", config)
    assert parsed == datetime(2026, 1, 12, 8, 30, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    ["", "   ", None, "yesterday", "2026-13-45T10:00:00", 12345],
)
def test_parse_time_rejects_invalid_values(config, value):
    with pytest.raises(ToolError) as exc:
        _poopjob.parse_time(value, config)
    assert "ISO 8601" in str(exc.value) or "time value is required" in str(exc.value)


def test_to_utc_iso_renders_the_api_format(config):
    parsed = _poopjob.parse_time("2026-01-12T10:30:00", config)
    assert _poopjob.to_utc_iso(parsed) == "2026-01-12T08:30:00Z"


def test_to_utc_iso_refuses_a_naive_time():
    with pytest.raises(ToolError):
        _poopjob.to_utc_iso(datetime(2026, 1, 12, 8, 30))


def test_now_utc_is_aware_utc():
    now = _poopjob.now_utc()
    assert now.tzinfo is timezone.utc
    assert abs((datetime.now(timezone.utc) - now).total_seconds()) < 2


def test_localize_uses_the_config_timezone_offset(config):
    assert (
        _poopjob.localize("2026-01-12T08:30:00Z", config) == "2026-01-12T10:30:00+02:00"
    )
    assert (
        _poopjob.localize("2026-07-01T08:30:00Z", config) == "2026-07-01T11:30:00+03:00"
    )


# ---------------------------------------------------------------------------
# Kind registry.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "name,path,time_field,paged",
    [
        ("diapers", "/api/diapers", "occurredAt", True),
        ("breastfeeding", "/api/feedings", "startedAt", True),
        ("bottles", "/api/bottle-feedings", "occurredAt", True),
        ("spit_ups", "/api/spit-ups", "occurredAt", False),
        ("weights", "/api/weights", "occurredAt", False),
    ],
)
def test_kind_registry(name, path, time_field, paged):
    kind = _poopjob.KINDS[name]
    assert kind.path == path
    assert kind.time_field == time_field
    assert kind.paged is paged


def test_get_kind_normalizes_and_lists_valid_names():
    assert _poopjob.get_kind("  Diapers ").name == "diapers"
    with pytest.raises(ToolError) as exc:
        _poopjob.get_kind("nappies")
    message = str(exc.value)
    assert "nappies" in message
    for name in _poopjob.KIND_NAMES:
        assert name in message


# ---------------------------------------------------------------------------
# HTTP helper: auth, success, error mapping, no retries, deadline, secrets.
# ---------------------------------------------------------------------------
def test_successful_post_sends_bearer_auth(config, http):
    http.response = FakeResponse(status_code=201, body={"id": "d1"})
    result = call_api(
        config, http, method="POST", body={"type": "poop", "occurredAt": "x"}
    )
    assert result == {"id": "d1"}
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://my.poopjob.app/api/diapers"
    assert call["headers"]["Authorization"] == "Bearer pj_synthetic_key"
    assert call["json"] == {"type": "poop", "occurredAt": "x"}
    # The timeout is a (connect, read) pair so a call cannot spend the
    # remaining budget twice.
    connect, read = call["timeout"]
    assert connect == _poopjob.CONNECT_TIMEOUT_SECONDS
    assert 0 < read <= _poopjob.API_PATH_DEADLINE_SECONDS


def test_redirect_is_not_followed_and_is_reported_as_an_error(config, http):
    http.response = FakeResponse(
        status_code=307,
        body=None,
        text="",
        headers={"Location": "https://elsewhere.example/collect"},
    )
    with pytest.raises(ToolError) as exc:
        call_api(config, http, method="POST", body={"type": "poop"})
    assert exc.value.status == 307
    assert "HTTP 307" in str(exc.value)
    # Exactly one request: a redirect must never replay the POST.
    assert len(http.calls) == 1
    assert http.calls[0]["allow_redirects"] is False


def test_get_passes_query_parameters_and_no_body(config, http):
    http.response = FakeResponse(body=[])
    result = call_api(
        config, http, params={"limit": 20, "before": "2026-01-01T00:00:00Z"}
    )
    assert result == []
    call = http.calls[0]
    assert call["params"] == {"limit": 20, "before": "2026-01-01T00:00:00Z"}
    assert call["json"] is None
    assert "Content-Type" not in call["headers"]


def test_error_mapping_surfaces_status_message_and_details(config, http):
    http.response = FakeResponse(
        status_code=400,
        body={
            "error": "validation_error",
            "message": "The body is invalid.",
            "details": {"volumeMl": "must be at most 500"},
        },
    )
    with pytest.raises(ToolError) as exc:
        call_api(config, http, method="POST", body={"volumeMl": 900})
    message = str(exc.value)
    assert "HTTP 400" in message
    assert "validation_error" in message
    assert "The body is invalid." in message
    assert "volumeMl" in message
    assert "must be at most 500" in message
    assert exc.value.status == 400


def test_rate_limit_includes_retry_after_and_is_not_retried(config, http):
    http.response = FakeResponse(
        status_code=429,
        body={"error": "rate_limited", "message": "Too many requests."},
        headers={"Retry-After": "30"},
    )
    with pytest.raises(ToolError) as exc:
        call_api(config, http, method="POST", body={"type": "poop"})
    message = str(exc.value)
    assert "HTTP 429" in message
    assert "30 seconds" in message
    assert len(http.calls) == 1


@pytest.mark.parametrize("body", [None, []])
def test_rate_limit_without_a_usable_body_still_includes_retry_after(
    config, http, body
):
    http.response = FakeResponse(
        status_code=429,
        body=body,
        headers={"Retry-After": "12"},
    )
    with pytest.raises(ToolError) as exc:
        call_api(config, http, method="POST", body={"type": "poop"})
    message = str(exc.value)
    assert "HTTP 429" in message
    assert "Retry after 12 seconds." in message
    assert exc.value.status == 429
    assert len(http.calls) == 1


def test_non_numeric_retry_after_falls_back_to_retry_later(config, http):
    http.response = FakeResponse(
        status_code=429,
        body={"error": "rate_limited"},
        headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
    )
    with pytest.raises(ToolError) as exc:
        call_api(config, http, method="POST", body={"type": "poop"})
    message = str(exc.value)
    assert "HTTP 429" in message
    assert "Retry later." in message
    assert "Retry after" not in message


def test_non_json_error_body_is_still_surfaced(config, http):
    http.response = FakeResponse(status_code=502, body=None, text="bad gateway")
    with pytest.raises(ToolError) as exc:
        call_api(config, http)
    message = str(exc.value)
    assert "HTTP 502" in message
    assert "bad gateway" in message


def test_transport_failure_is_classified_and_not_retried(config, http):
    http.response = requests.exceptions.ConnectionError("boom")
    with pytest.raises(ToolError) as exc:
        call_api(config, http, method="POST", body={"type": "poop"})
    assert "Could not reach the PoopJob API" in str(exc.value)
    assert "connection failed" in str(exc.value)
    assert exc.value.status is None
    assert len(http.calls) == 1


def test_transport_exception_never_leaks_the_api_key(config, http):
    leaky = requests.exceptions.InvalidHeader(f"Invalid header: {config.api_key}")
    http.response = leaky
    with pytest.raises(ToolError) as exc:
        call_api(config, http)
    printed = "".join(
        traceback.format_exception(type(exc.value), exc.value, exc.value.__traceback__)
    )
    assert config.api_key not in str(exc.value)
    assert config.api_key not in printed


def test_error_body_that_echoes_the_key_is_redacted(config, http):
    http.response = FakeResponse(
        status_code=500,
        body=None,
        text=f"Authorization: Bearer {config.api_key}",
    )
    with pytest.raises(ToolError) as exc:
        call_api(config, http)
    message = str(exc.value)
    assert config.api_key not in message
    assert "[redacted]" in message


def test_invalid_json_success_body_fails_loudly(config, http):
    http.response = FakeResponse(status_code=200, body=None, text="not json")
    with pytest.raises(ToolError) as exc:
        call_api(config, http)
    assert "not valid JSON" in str(exc.value)


def test_empty_success_body_returns_none(config, http):
    http.response = FakeResponse(status_code=204, body=None, text="")
    assert call_api(config, http, method="DELETE") is None


def test_expired_deadline_aborts_before_any_request(config, http):
    with pytest.raises(ToolError) as exc:
        _poopjob.api_request(
            "GET",
            "/api/diapers",
            config=config,
            deadline=time.monotonic() - 1,
        )
    assert "budget" in str(exc.value)
    assert http.calls == []


# ---------------------------------------------------------------------------
# config.json loading and validation.
# ---------------------------------------------------------------------------
def test_load_config_accepts_a_valid_config(isolated_root):
    (isolated_root / "config.json").write_text(
        json.dumps({"api_key": "pj_x", "timezone": "Europe/Athens"})
    )
    loaded = _poopjob.load_config()
    assert loaded.api_key == "pj_x"
    assert loaded.timezone == "Europe/Athens"
    assert loaded.tz == ZoneInfo("Europe/Athens")


def test_load_config_trims_surrounding_whitespace(isolated_root):
    (isolated_root / "config.json").write_text(
        json.dumps({"api_key": " pj_x ", "timezone": " Europe/Athens "})
    )
    loaded = _poopjob.load_config()
    assert loaded.api_key == "pj_x"
    assert loaded.timezone == "Europe/Athens"


@pytest.mark.parametrize(
    "contents,expected",
    [
        ('{"timezone": "Europe/Athens"}', "No PoopJob API key"),
        ('{"api_key": "   ", "timezone": "Europe/Athens"}', "No PoopJob API key"),
        ('{"api_key": "pj_x"}', "No timezone"),
        ('{"api_key": "pj_x", "timezone": "  "}', "No timezone"),
        ('{"api_key": "pj_x", "timezone": "Mars/Olympus"}', "IANA timezone"),
        ('{"api_key": "pj_x", "timezone": "not a zone"}', "IANA timezone"),
        ("[]", "JSON object"),
        ("{ not json", "Could not read config.json"),
    ],
)
def test_load_config_failures_are_clear_and_never_echo_the_key(
    isolated_root, contents, expected
):
    (isolated_root / "config.json").write_text(contents)
    with pytest.raises(ToolError) as exc:
        _poopjob.load_config()
    assert expected in str(exc.value)
    assert "pj_x" not in str(exc.value)


def test_load_config_missing_file_fails(isolated_root):
    with pytest.raises(ToolError) as exc:
        _poopjob.load_config()
    assert "Could not read config.json" in str(exc.value)


# ---------------------------------------------------------------------------
# Entry formatting.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind,entry,expected",
    [
        (
            "diapers",
            {
                "id": "d1",
                "type": "poop",
                "color": "mustard",
                "occurredAt": "2026-01-12T08:30:00Z",
                "createdAt": "2026-01-12T08:31:00Z",
            },
            {
                "id": "d1",
                "type": "poop",
                "color": "mustard",
                "occurred_at": "2026-01-12T10:30:00+02:00",
            },
        ),
        (
            "bottles",
            {
                "id": "b1",
                "occurredAt": "2026-01-12T08:30:00Z",
                "volumeMl": 90,
                "contents": "formula",
                "createdAt": "2026-01-12T08:31:00Z",
            },
            {
                "id": "b1",
                "occurred_at": "2026-01-12T10:30:00+02:00",
                "volume_ml": 90,
                "contents": "formula",
            },
        ),
        (
            "spit_ups",
            {
                "id": "s1",
                "occurredAt": "2026-01-12T08:30:00Z",
                "size": "small",
                "createdAt": "2026-01-12T08:31:00Z",
            },
            {
                "id": "s1",
                "occurred_at": "2026-01-12T10:30:00+02:00",
                "size": "small",
            },
        ),
        (
            "weights",
            {
                "id": "w1",
                "occurredAt": "2026-01-12T08:30:00Z",
                "grams": 4200,
                "createdAt": "2026-01-12T08:31:00Z",
            },
            {
                "id": "w1",
                "occurred_at": "2026-01-12T10:30:00+02:00",
                "grams": 4200,
            },
        ),
    ],
)
def test_format_entry_localizes_and_drops_created_at(config, kind, entry, expected):
    assert _poopjob.format_entry(kind, entry, config) == expected


def test_format_breastfeeding_keeps_segments_and_counts_closed_minutes(config):
    entry = {
        "id": "f1",
        "startedAt": "2026-01-12T08:00:00Z",
        "endedAt": "2026-01-12T08:20:00Z",
        "segments": [
            {
                "id": "s1",
                "side": "left",
                "startedAt": "2026-01-12T08:00:00Z",
                "endedAt": "2026-01-12T08:10:00Z",
            },
            {
                "id": "s2",
                "side": "right",
                "startedAt": "2026-01-12T08:10:00Z",
                "endedAt": "2026-01-12T08:17:30Z",
            },
            {
                "id": "s3",
                "side": "left",
                "startedAt": "2026-01-12T08:18:00Z",
                "endedAt": None,
            },
        ],
        "createdAt": "2026-01-12T08:20:00Z",
    }
    formatted = _poopjob.format_entry("breastfeeding", entry, config)
    assert formatted == {
        "id": "f1",
        "started_at": "2026-01-12T10:00:00+02:00",
        "ended_at": "2026-01-12T10:20:00+02:00",
        "segments": [
            {
                "side": "left",
                "started_at": "2026-01-12T10:00:00+02:00",
                "ended_at": "2026-01-12T10:10:00+02:00",
            },
            {
                "side": "right",
                "started_at": "2026-01-12T10:10:00+02:00",
                "ended_at": "2026-01-12T10:17:30+02:00",
            },
            {
                "side": "left",
                "started_at": "2026-01-12T10:18:00+02:00",
                "ended_at": None,
            },
        ],
        "left_minutes": 10.0,
        "right_minutes": 7.5,
        "total_minutes": 17.5,
    }


def test_format_breastfeeding_with_only_open_segments_has_zero_minutes(config):
    formatted = _poopjob.format_entry(
        "breastfeeding",
        {
            "id": "f1",
            "startedAt": "2026-01-12T08:00:00Z",
            "endedAt": None,
            "segments": [
                {
                    "id": "s1",
                    "side": "left",
                    "startedAt": "2026-01-12T08:00:00Z",
                    "endedAt": None,
                }
            ],
        },
        config,
    )
    assert formatted["left_minutes"] == 0
    assert formatted["right_minutes"] == 0
    assert formatted["total_minutes"] == 0
    assert formatted["ended_at"] is None
    assert formatted["segments"] == [
        {
            "side": "left",
            "started_at": "2026-01-12T10:00:00+02:00",
            "ended_at": None,
        }
    ]


def test_format_entry_rejects_unknown_kind_and_non_object(config):
    with pytest.raises(ToolError):
        _poopjob.format_entry("nappies", {}, config)
    with pytest.raises(ToolError):
        _poopjob.format_entry("diapers", [], config)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
