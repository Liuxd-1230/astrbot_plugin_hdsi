"""Timezone helpers. Port of HDS-Interlude src/time.ts.

Uses zoneinfo instead of Intl.DateTimeFormat; output fields keep identical
semantics so prompt payloads remain byte-compatible in meaning.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@lru_cache(maxsize=512)
def resolve_timezone(timezone_name: str) -> str:
    candidate = (timezone_name or "").strip() or "UTC"
    try:
        ZoneInfo(candidate)
        return candidate
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return "UTC"


def _tz(timezone_name: str) -> ZoneInfo:
    return ZoneInfo(resolve_timezone(timezone_name))


_WEEKDAY_EN = [
    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
]

_PERIOD_ZH = {
    "morning": "上午",
    "afternoon": "下午",
    "evening": "傍晚/晚上",
    "night": "夜间",
}


def _format_offset(dt_local: datetime) -> str:
    """Render UTC offset like Intl shortOffset, e.g. GMT+8 / GMT+5:30."""
    offset = dt_local.utcoffset() or _ZERO
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    minutes_abs = abs(total_minutes)
    hours, minutes = divmod(minutes_abs, 60)
    if minutes == 0:
        return f"GMT{sign}{hours}"
    return f"GMT{sign}{hours}:{minutes:02d}"


_ZERO = timezone.utc.utcoffset(None)  # type: ignore[arg-type]


def story_local_time_context(value: datetime, timezone_name: str) -> dict[str, Any]:
    resolved = resolve_timezone(timezone_name)
    local = value.astimezone(_tz(resolved))
    hour = local.hour
    if 5 <= hour < 12:
        period = "morning"
    elif 12 <= hour < 18:
        period = "afternoon"
    elif 18 <= hour < 22:
        period = "evening"
    else:
        period = "night"
    period_zh = _PERIOD_ZH[period]
    if period in ("morning", "afternoon"):
        daylight = (
            "normally daylight unless current weather, season, or setting "
            "explicitly says otherwise"
        )
    elif period == "evening":
        daylight = "transitioning toward darkness; use the established season and setting"
    else:
        daylight = "normally dark outside unless the setting explicitly says otherwise"
    date = f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
    time_text = f"{hour:02d}:{local.minute:02d}:{local.second:02d}"
    weekday = _WEEKDAY_EN[local.weekday()]
    utc_value = value.astimezone(timezone.utc)
    return {
        "timezone": resolved,
        "utc": iso_utc(utc_value),
        "local": f"{date} {time_text}",
        "date": date,
        "time": time_text,
        "hour": hour,
        "weekday": weekday,
        "offset": _format_offset(local),
        "period": period,
        "periodZh": period_zh,
        "daylightExpectation": daylight,
    }


def iso_utc(value: datetime) -> str:
    text = value.astimezone(timezone.utc).isoformat()
    return text.replace("+00:00", "Z")


def format_log_time(value: datetime | None, timezone_name: str) -> str:
    from .types import parse_date

    date = parse_date(value) if not isinstance(value, datetime) else value
    if date is None:
        return "-"
    try:
        local = date.astimezone(_tz(timezone_name))
    except Exception:
        local = date.astimezone(timezone.utc)
    return f"{local.month:02d}-{local.day:02d} {local.hour:02d}:{local.minute:02d}:{local.second:02d}"


def local_clock_minutes(value: datetime, timezone_name: str) -> int:
    """Minutes since local midnight; used by rest windows."""
    local = value.astimezone(_tz(timezone_name))
    return local.hour * 60 + local.minute


def calendar_day_key(value: datetime, timezone_name: str) -> str:
    """Local calendar day key (YYYY-MM-DD); used for overlay evidence days."""
    local = value.astimezone(_tz(timezone_name))
    return f"{local.year:04d}-{local.month:02d}-{local.day:02d}"
