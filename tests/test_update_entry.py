#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "pytest", "tzdata"]
# ///
"""Mocked checks for the update_entry tool.

The tests exercise the kind-specific body building, the breastfeeding segment
replacement, and every local rejection that must happen before a request is
sent. Config is built in memory and requests.request is a fake, so nothing
reads config.json or touches the network.

Run with: uv run --with pytest --with requests pytest
"""

import importlib.util
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

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


update_entry = _load_tool("update_entry")


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
    def __init__(self):
        self.calls = []
        self.handler = lambda method, url, kwargs: FakeResponse(body={})

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


def call(params, config, http, response):
    http.handler = lambda method, url, kwargs: FakeResponse(body=response)
    return update_entry.update_entry(params, config, _poopjob.api_deadline())


UPDATED = {
    "diapers": {
        "id": "d1",
        "type": "poop",
        "color": "mustard",
        "occurredAt": "2026-01-12T08:30:00Z",
        "createdAt": "2026-01-12T08:31:00Z",
    },
    "breastfeeding": {
        "id": "f1",
        "startedAt": "2026-01-12T08:00:00Z",
        "endedAt": "2026-01-12T08:25:00Z",
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
                "endedAt": "2026-01-12T08:25:00Z",
            },
        ],
        "createdAt": "2026-01-12T08:26:00Z",
    },
    "bottles": {
        "id": "b1",
        "occurredAt": "2026-01-12T08:30:00Z",
        "volumeMl": 120,
        "contents": "formula",
        "createdAt": "2026-01-12T08:31:00Z",
    },
    "spit_ups": {
        "id": "s1",
        "occurredAt": "2026-01-12T08:30:00Z",
        "size": "large",
        "createdAt": "2026-01-12T08:31:00Z",
    },
    "weights": {
        "id": "w1",
        "occurredAt": "2026-01-12T08:30:00Z",
        "grams": 4500,
        "createdAt": "2026-01-12T08:31:00Z",
    },
}


@pytest.mark.parametrize(
    "params,expected_body",
    [
        (
            {"kind": "diapers", "id": "d1", "type": "pee"},
            {"type": "pee"},
        ),
        (
            {
                "kind": "diapers",
                "id": "d1",
                "color": "none",
            },
            {"color": None},
        ),
        (
            {"kind": "diapers", "id": "d1", "color": " Mustard "},
            {"color": "mustard"},
        ),
        (
            {"kind": "diapers", "id": "d1", "occurred_at": "2026-01-12T10:30:00"},
            {"occurredAt": "2026-01-12T08:30:00Z"},
        ),
        (
            {
                "kind": "bottles",
                "id": "b1",
                "occurred_at": "2026-01-12T10:30:00",
                "volume_ml": 120,
                "contents": "formula",
            },
            {
                "occurredAt": "2026-01-12T08:30:00Z",
                "volumeMl": 120,
                "contents": "formula",
            },
        ),
        (
            {"kind": "spit_ups", "id": "s1", "size": "large"},
            {"size": "large"},
        ),
        (
            {"kind": "weights", "id": "w1", "grams": 4500},
            {"grams": 4500},
        ),
    ],
)
def test_patch_body_only_contains_the_kind_fields(config, http, params, expected_body):
    kind = _poopjob.get_kind(params["kind"])
    result = call(params, config, http, UPDATED[kind.name])

    call_args = http.calls[0]
    assert call_args["method"] == "PATCH"
    assert call_args["url"] == f"https://my.poopjob.app{kind.path}/{params['id']}"
    assert call_args["json"] == expected_body
    assert call_args["params"] is None

    assert result["kind"] == kind.name
    # The API's createdAt is bookkeeping and must not reach the assistant.
    assert "created_at" not in result["entry"]
    assert "createdAt" not in result["entry"]
    assert result["entry"]["id"] == params["id"]


def test_explicit_null_color_clears_it(config, http):
    call(
        {"kind": "diapers", "id": "d1", "color": None}, config, http, UPDATED["diapers"]
    )
    assert http.calls[0]["json"] == {"color": None}


def test_breastfeeding_segments_are_replaced_and_session_bounds_recomputed(
    config, http
):
    params = {
        "kind": "breastfeeding",
        "id": "f1",
        "segments": [
            {
                "side": "left",
                "started_at": "2026-01-12T10:00:00",
                "ended_at": "2026-01-12T10:10:00",
            },
            {
                "side": "right",
                "started_at": "2026-01-12T10:10:00",
                "ended_at": "2026-01-12T10:25:00",
            },
        ],
    }

    result = call(params, config, http, UPDATED["breastfeeding"])

    assert http.calls[0]["json"] == {
        "segments": [
            {
                "side": "left",
                "startedAt": "2026-01-12T08:00:00Z",
                "endedAt": "2026-01-12T08:10:00Z",
            },
            {
                "side": "right",
                "startedAt": "2026-01-12T08:10:00Z",
                "endedAt": "2026-01-12T08:25:00Z",
            },
        ],
        "startedAt": "2026-01-12T08:00:00Z",
        "endedAt": "2026-01-12T08:25:00Z",
    }
    assert result["entry"]["total_minutes"] == 25.0


def test_segments_are_unordered_but_session_bounds_use_earliest_and_latest(
    config, http
):
    params = {
        "kind": "breastfeeding",
        "id": "f1",
        "segments": [
            {
                "side": "right",
                "started_at": "2026-01-12T10:20:00",
                "ended_at": "2026-01-12T10:35:00",
            },
            {
                "side": "left",
                "started_at": "2026-01-12T10:00:00",
                "ended_at": "2026-01-12T10:10:00",
            },
        ],
    }

    call(params, config, http, UPDATED["breastfeeding"])

    body = http.calls[0]["json"]
    assert body["startedAt"] == "2026-01-12T08:00:00Z"
    assert body["endedAt"] == "2026-01-12T08:35:00Z"


def test_entry_id_is_url_quoted(config, http):
    call(
        {"kind": "diapers", "id": "a/b c", "type": "poop"},
        config,
        http,
        UPDATED["diapers"],
    )
    assert http.calls[0]["url"].endswith("/api/diapers/a%2Fb%20c")


@pytest.mark.parametrize(
    "kind,field,value",
    [
        ("diapers", "grams", 4200),
        ("breastfeeding", "occurred_at", "2026-01-12T10:00:00"),
        ("bottles", "size", "small"),
        ("spit_ups", "volume_ml", 90),
        ("weights", "type", "poop"),
    ],
)
def test_fields_from_another_kind_are_rejected(config, http, kind, field, value):
    with pytest.raises(ToolError) as exc:
        update_entry.update_entry(
            {"kind": kind, "id": "x", field: value}, config, _poopjob.api_deadline()
        )
    message = str(exc.value)
    assert field in message
    assert kind in message
    assert "supports" in message
    assert http.calls == []


def test_a_call_with_no_fields_is_rejected(config, http):
    with pytest.raises(ToolError) as exc:
        update_entry.update_entry(
            {"kind": "diapers", "id": "d1"}, config, _poopjob.api_deadline()
        )
    assert "Nothing to update" in str(exc.value)
    assert "type" in str(exc.value)
    assert http.calls == []


def test_unknown_parameters_are_rejected(config, http):
    with pytest.raises(ToolError) as exc:
        update_entry.update_entry(
            {"kind": "diapers", "id": "d1", "colour": "mustard"},
            config,
            _poopjob.api_deadline(),
        )
    assert "colour" in str(exc.value)
    assert http.calls == []


@pytest.mark.parametrize(
    "params,expected",
    [
        (
            {"kind": "diapers", "id": "d1", "type": "  "},
            "Missing required parameter: type",
        ),
        ({"kind": "diapers", "id": "d1", "type": "nappy"}, "Unknown type 'nappy'"),
        (
            {"kind": "diapers", "id": "d1", "color": "  "},
            "Missing required parameter: color",
        ),
        (
            {"kind": "diapers", "id": "d1", "color": "purple"},
            "Unknown color 'purple'",
        ),
        (
            {"kind": "bottles", "id": "b1", "volume_ml": "90"},
            "'volume_ml' must be an integer",
        ),
        (
            {"kind": "bottles", "id": "b1", "volume_ml": 0},
            "'volume_ml' must be from 1 to 500",
        ),
        (
            {"kind": "bottles", "id": "b1", "volume_ml": 501},
            "'volume_ml' must be from 1 to 500",
        ),
        (
            {"kind": "bottles", "id": "b1", "contents": "milk"},
            "Unknown contents 'milk'",
        ),
        ({"kind": "spit_ups", "id": "s1", "size": "huge"}, "Unknown size 'huge'"),
        (
            {"kind": "weights", "id": "w1", "grams": True},
            "'grams' must be an integer",
        ),
        (
            {"kind": "weights", "id": "w1", "grams": 299},
            "'grams' must be from 300 to 50000",
        ),
        (
            {"kind": "weights", "id": "w1", "grams": 50001},
            "'grams' must be from 300 to 50000",
        ),
    ],
)
def test_scalar_values_are_validated(config, http, params, expected):
    with pytest.raises(ToolError) as exc:
        update_entry.update_entry(params, config, _poopjob.api_deadline())
    assert expected in str(exc.value)
    assert http.calls == []


@pytest.mark.parametrize(
    "segments,expected",
    [
        ("not a list", "must be an array of segments"),
        ([], "At least one segment"),
        (["not an object"], "must be an object"),
        (
            [{"side": "left", "started_at": "2026-01-12T10:00:00"}],
            "ended_at is required",
        ),
        (
            [
                {
                    "side": "left",
                    "started_at": "2026-01-12T10:10:00",
                    "ended_at": "2026-01-12T10:00:00",
                }
            ],
            "must end after it starts",
        ),
        (
            [
                {
                    "side": "up",
                    "started_at": "2026-01-12T10:00:00",
                    "ended_at": "2026-01-12T10:10:00",
                }
            ],
            "Use left or right",
        ),
        (
            [
                {
                    "side": "left",
                    "start": "2026-01-12T10:00:00",
                    "end": "2026-01-12T10:10:00",
                }
            ],
            "unknown fields",
        ),
    ],
)
def test_invalid_segments_are_rejected(config, http, segments, expected):
    with pytest.raises(ToolError) as exc:
        update_entry.update_entry(
            {"kind": "breastfeeding", "id": "f1", "segments": segments},
            config,
            _poopjob.api_deadline(),
        )
    assert expected in str(exc.value)
    assert http.calls == []


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"id": "d1", "type": "poop"}, "Unknown entry kind"),
        ({"kind": "nappies", "id": "d1", "type": "poop"}, "nappies"),
        ({"kind": "diapers", "type": "poop"}, "id is required"),
        ({"kind": "diapers", "id": "  ", "type": "poop"}, "id is required"),
    ],
)
def test_missing_kind_or_id_is_rejected(config, http, params, expected):
    with pytest.raises(ToolError) as exc:
        update_entry.update_entry(params, config, _poopjob.api_deadline())
    assert expected in str(exc.value)
    assert http.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
