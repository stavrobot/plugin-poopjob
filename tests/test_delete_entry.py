#!/usr/bin/env -S uv run
# /// script
# dependencies = ["requests", "pytest", "tzdata"]
# ///
"""Mocked checks for the delete_entry tool.

Config lives in memory and requests.request is a fake, so nothing reads
config.json or reaches the API.

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


delete_entry = _load_tool("delete_entry")


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
        self.handler = lambda method, url, kwargs: FakeResponse(body={"ok": True})

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


def test_delete_returns_a_short_confirmation(config, http):
    result = delete_entry.delete_entry(
        {"kind": "diapers", "id": "d1"}, config, _poopjob.api_deadline()
    )

    assert result == {"deleted": True, "kind": "diapers", "id": "d1"}
    assert http.calls[0]["method"] == "DELETE"
    assert http.calls[0]["url"] == "https://my.poopjob.app/api/diapers/d1"
    assert http.calls[0]["json"] is None


def test_entry_id_is_url_quoted(config, http):
    delete_entry.delete_entry(
        {"kind": "bottles", "id": "a/b c"}, config, _poopjob.api_deadline()
    )
    assert http.calls[0]["url"].endswith("/api/bottle-feedings/a%2Fb%20c")


def test_active_breastfeeding_session_points_at_the_cancel_action(config, http):
    http.handler = lambda method, url, kwargs: FakeResponse(
        status_code=409,
        body={
            "error": "conflict",
            "message": "The active session cannot be deleted.",
        },
    )

    with pytest.raises(ToolError) as exc:
        delete_entry.delete_entry(
            {"kind": "breastfeeding", "id": "f1"}, config, _poopjob.api_deadline()
        )

    message = str(exc.value)
    assert "HTTP 409" in message
    assert "active breastfeeding session" in message
    assert "breastfeeding_timer" in message
    assert "cancel" in message


def test_a_conflict_on_another_kind_is_left_as_is(config, http):
    http.handler = lambda method, url, kwargs: FakeResponse(
        status_code=409, body={"error": "conflict", "message": "Nope."}
    )

    with pytest.raises(ToolError) as exc:
        delete_entry.delete_entry(
            {"kind": "diapers", "id": "d1"}, config, _poopjob.api_deadline()
        )

    assert "breastfeeding_timer" not in str(exc.value)
    assert "Nope." in str(exc.value)


def test_a_status_less_error_that_mentions_409_is_not_rewritten(config, monkeypatch):
    def fail(*args, **kwargs):
        raise ToolError("The PoopJob API returned HTTP 409: not from the API")

    monkeypatch.setattr(_poopjob, "api_request", fail)

    with pytest.raises(ToolError) as exc:
        delete_entry.delete_entry(
            {"kind": "breastfeeding", "id": "f1"}, config, _poopjob.api_deadline()
        )

    assert str(exc.value) == "The PoopJob API returned HTTP 409: not from the API"


def test_not_found_is_surfaced(config, http):
    http.handler = lambda method, url, kwargs: FakeResponse(
        status_code=404, body={"error": "not_found", "message": "No such entry."}
    )

    with pytest.raises(ToolError) as exc:
        delete_entry.delete_entry(
            {"kind": "weights", "id": "w1"}, config, _poopjob.api_deadline()
        )

    assert "HTTP 404" in str(exc.value)
    assert "No such entry." in str(exc.value)


@pytest.mark.parametrize(
    "params,expected",
    [
        ({"id": "d1"}, "Unknown entry kind"),
        ({"kind": "diapers"}, "id is required"),
        ({"kind": "diapers", "id": "  "}, "id is required"),
    ],
)
def test_missing_kind_or_id_is_rejected(config, http, params, expected):
    with pytest.raises(ToolError) as exc:
        delete_entry.delete_entry(params, config, _poopjob.api_deadline())
    assert expected in str(exc.value)
    assert http.calls == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
