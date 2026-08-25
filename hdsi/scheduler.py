"""Pure auto-advance scheduling helpers + background loop entry.

Port of the scheduling math from HDS-Interlude src/service.ts.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Optional

from .time import local_clock_minutes
from .types import InterludeStory, iso, parse_date

MINUTE = 60.0


def clock_minutes(value: str) -> Optional[int]:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if 0 <= hour < 24 and 0 <= minute < 60:
        return hour * 60 + minute
    return None


def active_rest_window(
    windows: list[dict[str, Any]],
    timezone_name: str,
    now: datetime,
) -> Optional[dict[str, Any]]:
    local_minutes = local_clock_minutes(now, timezone_name)
    for window in windows:
        if not window.get("enabled", True):
            continue
        start = clock_minutes(window.get("start", ""))
        end = clock_minutes(window.get("end", ""))
        if start is None or end is None:
            continue
        if start <= end:
            inside = start <= local_minutes < end
        else:
            inside = local_minutes >= start or local_minutes < end
        if inside:
            return window
    return None


def random_integer(minimum: int, maximum: int) -> int:
    lower = int(min(minimum, maximum))
    upper = int(max(minimum, maximum))
    return random.randint(lower, upper)


def automatic_interval_minutes(story: InterludeStory, now: datetime, config: dict[str, Any]) -> int:
    rest_window = active_rest_window(config.get("rest_windows") or [], story.setting.timezone, now)
    if rest_window is not None:
        return random_integer(
            int(rest_window.get("min_interval_minutes", 120)),
            int(rest_window.get("max_interval_minutes", 240)),
        )
    interval = max(1, int(config.get("interval_minutes", 40)))
    jitter = int(config.get("jitter_minutes", 5))
    return max(1, interval + random_integer(-jitter, jitter))


def normalize_follow_up_minutes(values: Any) -> list[int]:
    defaults = [10, 20]
    raw = values if isinstance(values, list) else defaults
    normalized: set[int] = set()
    for value in raw:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 240:
            normalized.add(number)
    return sorted(normalized)[:6]


def schedule_conversation_follow_ups(anchor: datetime, config: dict[str, Any]) -> list[datetime]:
    minutes_list = normalize_follow_up_minutes(config.get("follow_up_minutes"))
    jitter = min(10, max(0, int(config.get("follow_up_jitter_minutes", 1))))
    out: list[datetime] = []
    previous = anchor.timestamp()
    for minutes in minutes_list:
        offset = random_integer(-jitter, jitter) if jitter else 0
        at = max(previous + 1.0, anchor.timestamp() + max(1, minutes + offset) * MINUTE)
        previous = at
        out.append(datetime.fromtimestamp(at, tz=anchor.tzinfo))
    return out


def is_automatic_advance_paused(story: InterludeStory, now: datetime) -> bool:
    quiet_until = parse_date(story.state.automation.quiet_until)
    return quiet_until is not None and quiet_until > now


def due_conversation_follow_ups(story: InterludeStory, now: datetime) -> list[datetime]:
    planned = [
        value for value in (
            parse_date(text) for text in story.state.automation.conversation_follow_up_at
        )
        if value is not None
    ]
    planned.sort()
    return [value for value in planned if value <= now]


def is_automatic_advance_due(story: InterludeStory, now: datetime, config: dict[str, Any]) -> bool:
    if not config.get("enabled", True):
        return False
    scheduled = parse_date(story.state.automation.next_advance_at)
    if scheduled is not None:
        return scheduled <= now
    interval_seconds = max(1, int(config.get("interval_minutes", 40))) * MINUTE
    cursor = story.cursor_at
    if cursor.tzinfo is None:
        from datetime import timezone as _tz

        cursor = cursor.replace(tzinfo=_tz.utc)
    return (now - cursor).total_seconds() >= interval_seconds
