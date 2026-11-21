"""Shared internals for the PoopJob plugin tools.

PoopJob is a baby-tracking app with a small REST API. Every tool needs the
same handful of things: an API key and a timezone from config.json, a single
HTTP helper with one shared time budget, conversion between the local
timezone and the UTC timestamps the API uses, and a formatter that turns an
API entry into the compact shape the assistant sees.

Tools import this module by inserting the plugin root on sys.path, the same
way they locate ../config.json:

    import pathlib
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    import _poopjob
    from _poopjob import ToolError

The HTTP helper never retries, and that is deliberate: creates are not
idempotent, so repeating a POST after a timeout can log the same diaper or
bottle twice. A failed call is reported to the assistant, which can inspect
the log before deciding to try again.

Tool output uses snake_case keys with API values unchanged, and timestamps
localized to the configured timezone:

    diaper     {id, type, color, occurred_at}
    bottle     {id, occurred_at, volume_ml, contents}
    spit_up    {id, occurred_at, size}
    weight     {id, occurred_at, grams}
    feeding    {id, started_at, ended_at, segments, left_minutes,
                right_minutes, total_minutes}

A feeding segment is {side, started_at, ended_at}; ended_at is null while the
segment is still open. Minute counts only include closed segments.
"""

import json
import pathlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

ROOT = pathlib.Path(__file__).resolve().parent

API_BASE_URL = "https://my.poopjob.app"

# Both tools and this module must fit the runner's hard ~30s kill. requests
# applies its connect and read timeouts separately, so a single float would let
# one request spend the remaining budget twice. Every request therefore gets a
# (connect, read) pair derived from the time left under one overall deadline
# that the calling tool starts once and threads through every api_request()
# call.
API_PATH_DEADLINE_SECONDS = 20
CONNECT_TIMEOUT_SECONDS = 5


class ToolError(Exception):
    """Fatal, user-facing error whose message is safe to print.

    'status' is the HTTP status when the API returned an error, and None for a
    local validation failure or a transport error. Tools use it to react to a
    specific HTTP outcome without matching text in the message.
    """

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Config:
    """Validated plugin configuration.

    'timezone' is the IANA name as configured, kept for messages; 'tz' is the
    parsed zone used for conversions.
    """

    api_key: str
    timezone: str
    tz: ZoneInfo


@dataclass(frozen=True)
class Kind:
    """One log kind: where it lives, which field orders it, and how to read it.

    'time_field' is the API timestamp that orders a list and that list_entries
    passes as 'before'. 'paged' says whether the list endpoint accepts
    'before'; the spit-up and weight lists do not, so they are fetched whole
    and filtered locally. 'timestamp_fields' are the entry fields the
    formatter localizes.
    """

    name: str
    path: str
    time_field: str
    paged: bool
    timestamp_fields: tuple[str, ...]
    has_segments: bool = False


KINDS = {
    "diapers": Kind(
        name="diapers",
        path="/api/diapers",
        time_field="occurredAt",
        paged=True,
        timestamp_fields=("occurredAt",),
    ),
    "breastfeeding": Kind(
        name="breastfeeding",
        path="/api/feedings",
        time_field="startedAt",
        paged=True,
        timestamp_fields=("startedAt", "endedAt"),
        has_segments=True,
    ),
    "bottles": Kind(
        name="bottles",
        path="/api/bottle-feedings",
        time_field="occurredAt",
        paged=True,
        timestamp_fields=("occurredAt",),
    ),
    "spit_ups": Kind(
        name="spit_ups",
        path="/api/spit-ups",
        time_field="occurredAt",
        paged=False,
        timestamp_fields=("occurredAt",),
    ),
    "weights": Kind(
        name="weights",
        path="/api/weights",
        time_field="occurredAt",
        paged=False,
        timestamp_fields=("occurredAt",),
    ),
}

KIND_NAMES = tuple(KINDS)

# API field -> output field, in the order the assistant should see them.
# createdAt is deliberately absent: it is bookkeeping, not something an
# assistant acts on, and tool output costs conversation context.
_KIND_FIELDS = {
    "diapers": (
        ("id", "id"),
        ("type", "type"),
        ("color", "color"),
        ("occurredAt", "occurred_at"),
    ),
    "breastfeeding": (
        ("id", "id"),
        ("startedAt", "started_at"),
        ("endedAt", "ended_at"),
    ),
    "bottles": (
        ("id", "id"),
        ("occurredAt", "occurred_at"),
        ("volumeMl", "volume_ml"),
        ("contents", "contents"),
    ),
    "spit_ups": (
        ("id", "id"),
        ("occurredAt", "occurred_at"),
        ("size", "size"),
    ),
    "weights": (
        ("id", "id"),
        ("occurredAt", "occurred_at"),
        ("grams", "grams"),
    ),
}


def load_config() -> Config:
    """Read and validate config.json, or fail with a user-facing message.

    The API key is never included in an error or in any other output; only
    the name of the setting is mentioned.
    """
    try:
        with (ROOT / "config.json").open() as config_file:
            raw = json.load(config_file)
    except (OSError, ValueError) as err:
        raise ToolError(f"Could not read config.json: {err}") from err
    if not isinstance(raw, dict):
        raise ToolError("config.json must contain a JSON object.")

    api_key = raw.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ToolError(
            "No PoopJob API key found in config.json. Set api_key to a key "
            "created in the PoopJob app under Settings > API keys; it starts "
            "with 'pj_'."
        )

    timezone_name = raw.get("timezone")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        raise ToolError(
            "No timezone found in config.json. Set timezone to an IANA name "
            "such as 'Europe/Athens'."
        )
    timezone_name = timezone_name.strip()
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as err:
        raise ToolError(
            f"'{timezone_name}' is not a valid IANA timezone. Set timezone in "
            "config.json to a name such as 'Europe/Athens'."
        ) from err

    return Config(api_key=api_key.strip(), timezone=timezone_name, tz=tz)


# Known and accepted bound: requests applies its connect and read timeouts
# separately, so the worst case for one request is roughly the 20s deadline
# plus the 5s connect cap, about 25s, still under the runner's ~30s kill. A
# pathological server trickling its response body can exceed that, but the
# consequence is a runner timeout rather than a wrong answer, so that risk is
# accepted rather than overlooked. Do not "fix" it blindly; it needs a real
# wall-clock mechanism, not a smaller timeout.
def api_deadline() -> float:
    """Start the overall time budget for one tool call."""
    return time.monotonic() + API_PATH_DEADLINE_SECONDS


def api_timeout(deadline: float) -> tuple[float, float]:
    """Return the (connect, read) timeout pair for one request."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ToolError(
            "The PoopJob API calls exceeded the "
            f"{API_PATH_DEADLINE_SECONDS}-second tool budget and were "
            "aborted. Try again shortly."
        )
    return (min(CONNECT_TIMEOUT_SECONDS, remaining), remaining)


# Substituted for the API key when it appears in text derived from an API
# response. A response or an error message can echo the Authorization header,
# and any such text must be scrubbed before it reaches stderr or stdout.
_REDACTED = "[redacted]"


def redact_secret(text: str, secret: str) -> str:
    """Replace every occurrence of a secret with a placeholder.

    Redaction runs on the raw string before it is whitespace-normalized or
    truncated: normalizing first could split a key containing whitespace so a
    plain replace no longer matched it, and truncating first could leave a
    partial key behind.
    """
    if not secret:
        return text
    return text.replace(secret, _REDACTED)


def _body_snippet(response, secret: str) -> str:
    """A short, single-line excerpt of a failed response for error messages."""
    redacted = redact_secret(response.text or "", secret)
    text = " ".join(redacted.split())
    return text[:300] if text else "(empty response body)"


def _retry_after_suffix(response) -> str:
    """The 429 retry hint, whether or not the body is usable."""
    if response.status_code != 429:
        return ""
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        retry_after = str(retry_after).strip()
        if retry_after.isascii() and retry_after.isdigit():
            return f" Retry after {retry_after} seconds."
    return " Retry later."


def _error_message(response, api_key: str) -> str:
    """Turn a failed API response into one readable, redacted message.

    The API documents errors as {"error", "message", "details"}; details is
    present on validation failures. A body that is not that shape is still
    surfaced, because hiding it would leave the caller with only a status.
    """
    secret = api_key.strip()
    snippet = _body_snippet(response, secret)
    try:
        body = response.json()
    except ValueError:
        body = None

    if not isinstance(body, dict):
        return (
            f"The PoopJob API returned HTTP {response.status_code}: {snippet}"
            + _retry_after_suffix(response)
        )

    code = body.get("error")
    message = body.get("message")
    parts = [f"The PoopJob API returned HTTP {response.status_code}"]
    if isinstance(code, str) and code.strip():
        parts.append(f"({redact_secret(code, secret)})")
    text = " ".join(parts)

    if isinstance(message, str) and message.strip():
        text += f": {redact_secret(message, secret)}"

    details = body.get("details")
    if details not in (None, {}, []):
        rendered = json.dumps(details, ensure_ascii=False, sort_keys=True)
        rendered = redact_secret(rendered, secret)
        if len(rendered) > 500:
            rendered = rendered[:500] + "..."
        text += f" Details: {rendered}"

    if not (isinstance(message, str) and message.strip()) and details in (
        None,
        {},
        [],
    ):
        text += f": {snippet}"

    text += _retry_after_suffix(response)

    return text


def _transport_error_message(err: requests.exceptions.RequestException) -> str:
    """Classify a transport failure without echoing the exception text.

    requests exceptions can embed the request, and an API key with an
    embedded newline makes requests raise InvalidHeader with the whole key in
    the message. That text is unsafe to print, so only a fixed classification
    is returned.
    """
    if isinstance(err, requests.exceptions.Timeout):
        detail = "the request timed out"
    elif isinstance(err, requests.exceptions.ConnectionError):
        detail = "the connection failed"
    else:
        detail = "the request could not be completed"
    return f"Could not reach the PoopJob API: {detail}."


def api_request(
    method: str,
    path: str,
    *,
    config: Config,
    deadline: float,
    body: dict | None = None,
    params: dict | None = None,
) -> object:
    """Send exactly one request to the PoopJob API and return its JSON body.

    Auth is a bearer token. The timeout comes from the shared deadline, and
    nothing is retried: a POST is not idempotent, and even a GET retry would
    spend a rate-limit slot the caller has not agreed to spend.

    Every failure mode raises ToolError: transport failure, a non-2xx status,
    and a success body that is not valid JSON. An empty success body returns
    None.
    """
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Accept": "application/json",
    }
    if body is not None:
        headers["Content-Type"] = "application/json"

    try:
        response = requests.request(
            method,
            API_BASE_URL + path,
            headers=headers,
            params=params,
            json=body,
            timeout=api_timeout(deadline),
            # A redirect must never replay a POST, and any 3xx is treated as
            # an error below rather than silently followed.
            allow_redirects=False,
        )
    except requests.exceptions.RequestException as err:
        # from None drops the chain on purpose: the original exception can
        # carry the Authorization header, and a chained traceback would print
        # it. Only the fixed classification is safe to surface.
        raise ToolError(_transport_error_message(err)) from None

    if not 200 <= response.status_code < 300:
        raise ToolError(
            _error_message(response, config.api_key),
            status=response.status_code,
        )

    if not (response.text or "").strip():
        return None

    try:
        return response.json()
    except ValueError as err:
        raise ToolError(
            "The PoopJob API returned a response that was not valid JSON."
        ) from err


def _parse_iso(value: str) -> datetime:
    """Parse an ISO 8601 string without interpreting a missing offset.

    Python's fromisoformat() accepts a trailing 'Z' only from 3.11, and the
    runner's Python version is not ours to choose, so 'Z' is normalized first.
    """
    text = str(value).strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as err:
        raise ToolError(
            f"'{value}' is not a valid ISO 8601 time. Pass a time such as "
            "'2026-01-12T08:30:00+02:00' or '2026-01-12T08:30:00'."
        ) from err


def _is_naive(value: datetime) -> bool:
    return value.tzinfo is None or value.utcoffset() is None


def _local_naive_to_utc(value: str, naive: datetime, config: Config) -> datetime:
    """Interpret a wall-clock time in the configured zone, rejecting DST edges.

    A naive time can be missing entirely when clocks spring forward, or occur
    twice when they fall back. Guessing either way can log an entry an hour
    off, so the caller is asked for an explicit UTC offset instead.
    """
    first = naive.replace(tzinfo=config.tz)
    second = naive.replace(tzinfo=config.tz, fold=1)

    # A missing time does not survive a round trip through UTC: the local
    # clock has already moved past the requested wall time.
    round_trip = first.astimezone(timezone.utc).astimezone(config.tz)
    if round_trip.replace(tzinfo=None) != naive:
        raise ToolError(
            f"The time '{value}' does not exist in {config.timezone}: it falls "
            "in the hour skipped when daylight saving time begins. Pass the "
            "time with an explicit UTC offset, such as '+02:00' or '+03:00'."
        )

    # The two folds resolve to different offsets only when the wall time
    # occurs twice.
    if first.utcoffset() != second.utcoffset():
        raise ToolError(
            f"The time '{value}' is ambiguous in {config.timezone}: it falls "
            "in the hour repeated when daylight saving time ends. Pass the "
            "time with an explicit UTC offset, such as '+02:00' or '+03:00'."
        )

    return first.astimezone(timezone.utc)


def parse_time(value, config: Config) -> datetime:
    """Parse a tool-supplied time into an aware UTC datetime.

    A value with an offset is taken as that instant. A value without an offset
    is interpreted in the configured timezone, which is what "8:30 this
    morning" means to the person who typed it.
    """
    if not isinstance(value, str) or not value.strip():
        raise ToolError("A time value is required, in ISO 8601 format.")
    parsed = _parse_iso(value)
    if _is_naive(parsed):
        return _local_naive_to_utc(value, parsed, config)
    return parsed.astimezone(timezone.utc)


def to_utc_iso(value: datetime) -> str:
    """Render an aware datetime as the UTC ISO 8601 string the API expects."""
    if _is_naive(value):
        raise ToolError("Internal error: cannot convert a time without an offset.")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_api_time(value) -> datetime:
    """Parse an API timestamp into an aware UTC datetime.

    The API promises UTC, so a timestamp without an offset is taken as UTC
    rather than as the configured local time. Otherwise a malformed response
    could shift a filter or a page boundary by hours.
    """
    if not isinstance(value, str) or not value.strip():
        raise ToolError("The PoopJob API returned a missing timestamp.")
    parsed = _parse_iso(value)
    if _is_naive(parsed):
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_utc() -> datetime:
    """The current instant, in UTC, for entries logged without a time."""
    return datetime.now(timezone.utc)


def localize(value, config: Config) -> str:
    """Render an API timestamp in the configured timezone, with its offset."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError("The PoopJob API returned a missing timestamp.")
    parsed = _parse_iso(value)
    # The API promises UTC. A timestamp without an offset is therefore taken
    # as UTC rather than as local time, so a malformed response cannot shift
    # an entry by hours.
    if _is_naive(parsed):
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(config.tz).isoformat()


def get_kind(name) -> Kind:
    """Resolve a tool's kind name, or fail listing the valid names."""
    text = str(name or "").strip().lower()
    kind = KINDS.get(text)
    if kind is None:
        raise ToolError(
            f"Unknown entry kind '{name}'. Valid kinds: " + ", ".join(KIND_NAMES) + "."
        )
    return kind


# ---------------------------------------------------------------------------
# Tool parameter validation, shared so every tool rejects input the same way.
# ---------------------------------------------------------------------------
DIAPER_TYPES = ("poop", "pee", "mix")
DIAPER_COLORS = (
    "meconium",
    "transitional",
    "mustard",
    "yellowgreen",
    "tan",
    "pale",
    "red",
    "black",
)
BOTTLE_CONTENTS = ("breast_milk", "formula")
BOTTLE_VOLUME_MIN = 1
BOTTLE_VOLUME_MAX = 500
SPIT_UP_SIZES = ("small", "medium", "large")
WEIGHT_GRAMS_MIN = 300
WEIGHT_GRAMS_MAX = 50000
WEIGHT_GRAMS_HINT = (
    "Convert kilograms to grams by multiplying by 1000, and pounds by "
    "multiplying by 453.592."
)


def require_choice(value, name: str, choices) -> str:
    """Validate a required enum value, case-insensitively."""
    if not isinstance(value, str) or not value.strip():
        raise ToolError(
            f"Missing required parameter: {name}. One of: {', '.join(choices)}."
        )
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ToolError(
            f"Unknown {name} '{value}'. Valid values: {', '.join(choices)}."
        )
    return normalized


def optional_choice(value, name: str, choices):
    """Validate an optional enum value; absent or blank means unset."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ToolError(
            f"'{name}' must be a string. Valid values: {', '.join(choices)}."
        )
    if not value.strip():
        return None
    return require_choice(value, name, choices)


def require_integer(value, name: str, minimum: int, maximum: int, *, hint="") -> int:
    """Validate a required integer within an inclusive range.

    'hint' is appended to the errors that quote a bad value, which is where a
    tool adds unit-conversion advice.
    """
    if value is None:
        raise ToolError(
            f"Missing required parameter: {name} (an integer from {minimum} "
            f"to {maximum})."
        )
    # bool is an int subclass, but true/false is never a valid amount.
    suffix = f" {hint}" if hint else ""
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolError(
            f"'{name}' must be an integer from {minimum} to {maximum}, got "
            f"{value!r}.{suffix}"
        )
    if not minimum <= value <= maximum:
        raise ToolError(
            f"'{name}' must be from {minimum} to {maximum}, got {value}.{suffix}"
        )
    return value


def optional_time_or_now(value, config: Config) -> str:
    """Parse an optional time parameter as UTC ISO, defaulting to now.

    A blank string counts as absent, which is how a tool sees a parameter the
    assistant passed but left empty.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return to_utc_iso(now_utc())
    return to_utc_iso(parse_time(value, config))


# Both tools that accept a breastfeeding segment list use the same wording, so
# an error names the exact field at fault, such as segments[0].started_at.
SEGMENT_FIELDS = frozenset(("side", "started_at", "ended_at"))
SIDES = ("left", "right")


def parse_segments(value, config: Config):
    """Validate a supplied segment list and return its API shape and bounds.

    A logged or replaced session is in the past, so every segment must be
    closed: a missing ended_at is an error rather than something the API might
    reopen. Returns the segments in the API's shape plus every parsed start
    and end, from which the caller derives the session bounds.
    """
    if not isinstance(value, list):
        raise ToolError("'segments' must be an array of segments.")
    if not value:
        raise ToolError(
            "At least one segment is required, for example "
            '{"side": "left", "started_at": "...", "ended_at": "..."}.'
        )

    segments = []
    starts = []
    ends = []
    for index, raw in enumerate(value):
        label = f"segments[{index}]"
        if not isinstance(raw, dict):
            raise ToolError(f"{label} must be an object.")

        unknown = set(raw) - SEGMENT_FIELDS
        if unknown:
            keys = ", ".join(sorted(unknown))
            raise ToolError(
                f"{label} has unknown fields: {keys}. "
                "Use side, started_at and ended_at."
            )

        raw_side = raw.get("side")
        if not isinstance(raw_side, str) or not raw_side.strip():
            raise ToolError(f"{label}.side is required: left or right.")
        side = raw_side.strip().lower()
        if side not in SIDES:
            raise ToolError(f"Unknown {label}.side '{raw_side}'. Use left or right.")

        started_raw = raw.get("started_at")
        if not isinstance(started_raw, str) or not started_raw.strip():
            raise ToolError(f"{label}.started_at is required, in ISO 8601 format.")
        ended_raw = raw.get("ended_at")
        if not isinstance(ended_raw, str) or not ended_raw.strip():
            raise ToolError(
                f"{label}.ended_at is required, in ISO 8601 format. A segment "
                "in a finished session must be closed."
            )
        try:
            started = parse_time(started_raw, config)
        except ToolError as err:
            raise ToolError(f"{label}.started_at: {err}") from err
        try:
            ended = parse_time(ended_raw, config)
        except ToolError as err:
            raise ToolError(f"{label}.ended_at: {err}") from err

        if ended <= started:
            raise ToolError(f"{label} must end after it starts.")

        segments.append(
            {
                "side": side,
                "startedAt": to_utc_iso(started),
                "endedAt": to_utc_iso(ended),
            }
        )
        starts.append(started)
        ends.append(ended)

    return segments, starts, ends


def _format_segment(segment, config: Config) -> dict:
    if not isinstance(segment, dict):
        raise ToolError("The PoopJob API returned an unexpected feeding segment.")
    started = segment.get("startedAt")
    ended = segment.get("endedAt")
    return {
        "side": segment.get("side"),
        "started_at": localize(started, config) if started else None,
        "ended_at": localize(ended, config) if ended else None,
    }


def _segment_minutes(segments: list) -> dict:
    """Total each side's closed segments. Open segments contribute nothing."""
    seconds = {"left": 0.0, "right": 0.0}
    for segment in segments:
        if not isinstance(segment, dict):
            continue
        side = segment.get("side")
        started = segment.get("startedAt")
        ended = segment.get("endedAt")
        if side not in seconds or not started or not ended:
            continue
        start = _parse_iso(started)
        end = _parse_iso(ended)
        if _is_naive(start):
            start = start.replace(tzinfo=timezone.utc)
        if _is_naive(end):
            end = end.replace(tzinfo=timezone.utc)
        seconds[side] += max(0.0, (end - start).total_seconds())

    left = round(seconds["left"] / 60, 1)
    right = round(seconds["right"] / 60, 1)
    total = round((seconds["left"] + seconds["right"]) / 60, 1)
    return {"left_minutes": left, "right_minutes": right, "total_minutes": total}


def format_entry(kind_name, entry, config: Config) -> dict:
    """Reduce an API entry to the compact shape tool output returns.

    Timestamps are localized, createdAt is dropped, and a breastfeeding
    session gains its per-side and total minutes. The segment ids are dropped
    too; the assistant never needs them, and an unknown kind or a non-object
    entry is a loud failure rather than a silently empty result.
    """
    kind = get_kind(kind_name)
    if not isinstance(entry, dict):
        raise ToolError(f"The PoopJob API returned an unexpected {kind.name} entry.")

    result = {}
    for api_field, output_field in _KIND_FIELDS[kind.name]:
        value = entry.get(api_field)
        if api_field in kind.timestamp_fields and value is not None:
            value = localize(value, config)
        result[output_field] = value

    if kind.has_segments:
        segments = entry.get("segments")
        segments = segments if isinstance(segments, list) else []
        result["segments"] = [_format_segment(segment, config) for segment in segments]
        result.update(_segment_minutes(segments))

    return result
