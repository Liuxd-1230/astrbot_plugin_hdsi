"""Agency Window pure logic. Port of HDS-Interlude src/agency.ts.

Keeps the original capacity matrix, grounding rules, fingerprint dedup and
recheck-time selection exactly as in 0.1.3-beta1.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional
from urllib.parse import urlparse

from .types import (
    AGENCY_ACTIVITY_LOADS,
    AGENCY_DEVICE_ACCESS,
    AGENCY_PRIVACIES,
    PROACTIVE_ORIGINS,
    PROACTIVE_OUTCOMES,
    PROACTIVE_DISCLOSURES,
    AgencyConfig,
    AgencyWindowState,
    ProactiveContactDraft,
    clamp_number,
    iso,
    parse_date,
)

MINUTE = 60.0
HOUR = 60 * MINUTE

DEFAULT_AGENCY_CONFIG = AgencyConfig()


def resolve_agency_config(value: dict[str, Any] | None) -> AgencyConfig:
    if not isinstance(value, dict):
        return AgencyConfig()
    merged = DEFAULT_AGENCY_CONFIG.model_dump()
    alias = {
        "maxWindowMinutes": "max_window_minutes",
        "minimumProactiveIntervalMinutes": "minimum_proactive_interval_minutes",
        "maxCandidateHours": "max_candidate_hours",
    }
    for key in list(merged):
        if key in value:
            merged[key] = value[key]
    for old, new in alias.items():
        if old in value:
            merged[new] = value[old]
    try:
        return AgencyConfig(**merged)
    except Exception:
        return AgencyConfig()


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _positive_ids(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            number = int(item)
        except (TypeError, ValueError):
            continue
        # bool is an int subclass; reject it explicitly.
        if isinstance(item, bool) or not math.isfinite(number) or number <= 0 or number != int(number):
            continue
        out.append(number)
    return out


def _grounded_ids(
    value: Any,
    valid: Iterable[int] | frozenset[int],
    fallback: int | None = None,
) -> list[int]:
    valid_set = set(valid)
    ids = [entry_id for entry_id in _positive_ids(value) if entry_id in valid_set]
    if not ids and fallback and fallback > 0:
        ids.append(fallback)
    unique = list(dict.fromkeys(ids))
    return unique[-20:]


def normalize_agency_window_state(value: Any) -> Optional[AgencyWindowState]:
    if isinstance(value, AgencyWindowState):
        return value
    if not isinstance(value, dict):
        return None
    activity_load = value.get("activityLoad", value.get("activity_load"))
    privacy = value.get("privacy")
    device_access = value.get("deviceAccess", value.get("device_access"))
    if activity_load not in AGENCY_ACTIVITY_LOADS:
        return None
    if privacy not in AGENCY_PRIVACIES:
        return None
    if device_access not in AGENCY_DEVICE_ACCESS:
        return None
    valid_until = parse_date(value.get("validUntil", value.get("valid_until")))
    updated_at = parse_date(value.get("updatedAt", value.get("updated_at")))
    if valid_until is None or updated_at is None:
        return None
    next_opportunity = parse_date(value.get("nextOpportunityAt", value.get("next_opportunity_at")))
    return AgencyWindowState(
        activity_load=activity_load,  # type: ignore[arg-type]
        privacy=privacy,  # type: ignore[arg-type]
        device_access=device_access,  # type: ignore[arg-type]
        next_opportunity_at=iso(next_opportunity) if next_opportunity else None,
        valid_until=iso(valid_until),
        basis=_text(value.get("basis"), 500),
        source_entry_ids=_positive_ids(
            value.get("sourceEntryIds", value.get("source_entry_ids"))
        )[-20:],
        updated_at=iso(updated_at),
    )


def normalize_agency_window_draft(
    value: Any,
    now: datetime,
    config: AgencyConfig,
    valid_source_entry_ids: frozenset[int] | set[int],
    fallback_source_entry_id: int | None = None,
) -> Optional[AgencyWindowState]:
    """Validate a model-proposed agency window against real script entries."""
    if not isinstance(value, dict):
        return None
    activity_load = value.get("activityLoad", value.get("activity_load"))
    privacy = value.get("privacy")
    device_access = value.get("deviceAccess", value.get("device_access"))
    if activity_load not in AGENCY_ACTIVITY_LOADS:
        return None
    if privacy not in AGENCY_PRIVACIES:
        return None
    if device_access not in AGENCY_DEVICE_ACCESS:
        return None
    maximum = now + timedelta(minutes=max(5, config.max_window_minutes))
    requested_until = parse_date(value.get("validUntil", value.get("valid_until")))
    if requested_until is not None and requested_until > now:
        valid_until = min(requested_until, maximum)
    else:
        valid_until = maximum
    requested_opportunity = parse_date(value.get("nextOpportunityAt", value.get("next_opportunity_at")))
    next_opportunity_at: Optional[str] = None
    if requested_opportunity is not None and requested_opportunity > now:
        next_opportunity_at = iso(min(requested_opportunity, valid_until))
    source_entry_ids = _grounded_ids(
        value.get("sourceEntryIds", value.get("source_entry_ids")),
        valid_source_entry_ids,
        fallback_source_entry_id,
    )
    basis = _text(value.get("basis"), 500)
    if not basis or not source_entry_ids:
        return None
    return AgencyWindowState(
        activity_load=activity_load,  # type: ignore[arg-type]
        privacy=privacy,  # type: ignore[arg-type]
        device_access=device_access,  # type: ignore[arg-type]
        next_opportunity_at=next_opportunity_at,
        valid_until=iso(valid_until),
        basis=basis,
        source_entry_ids=source_entry_ids,
        updated_at=iso(now),
    )


def active_agency_window(
    value: Any,
    now: datetime | None = None,
) -> Optional[AgencyWindowState]:
    now = now or datetime.now(timezone.utc)
    state = normalize_agency_window_state(value)
    if state is None:
        return None
    valid_until = parse_date(state.valid_until)
    if valid_until is None or valid_until <= now:
        return None
    return state


def normalize_proactive_contact(
    value: Any,
    now: datetime,
    config: AgencyConfig,
    permitted_participant_ids: frozenset[str] | set[str],
    valid_source_entry_ids: frozenset[int] | set[int],
    fallback_source_entry_id: int | None = None,
) -> Optional[ProactiveContactDraft]:
    if not isinstance(value, dict):
        return None
    participant_id = str(value.get("participantId", value.get("participant_id")) or "")
    if participant_id not in permitted_participant_ids:
        return None
    origin = value.get("origin")
    disclosure = value.get("disclosure")
    outcome = value.get("outcome")
    if origin not in PROACTIVE_ORIGINS:
        return None
    if disclosure not in PROACTIVE_DISCLOSURES:
        return None
    if outcome not in PROACTIVE_OUTCOMES:
        return None
    motive = _text(value.get("motive"), 600)
    source_entry_ids = _grounded_ids(
        value.get("sourceEntryIds", value.get("source_entry_ids")),
        valid_source_entry_ids,
        fallback_source_entry_id,
    )
    if not motive or not source_entry_ids:
        return None
    maximum_expiry = now + timedelta(hours=max(1, config.max_candidate_hours))
    requested_expiry = parse_date(value.get("expiresAt", value.get("expires_at")))
    expires_at = (
        min(requested_expiry, maximum_expiry)
        if requested_expiry is not None and requested_expiry > now
        else maximum_expiry
    )
    requested_not_before = parse_date(value.get("notBefore", value.get("not_before")))
    not_before: Optional[str] = None
    if (
        requested_not_before is not None
        and now < requested_not_before < expires_at
    ):
        not_before = iso(requested_not_before)
    willingness_raw = value.get("willingness")
    willingness: Optional[float] = None
    if isinstance(willingness_raw, (int, float)) and not isinstance(willingness_raw, bool):
        if math.isfinite(willingness_raw):
            willingness = clamp_number(willingness_raw, 0, 0, 1)
    return ProactiveContactDraft(
        participant_id=participant_id,
        origin=origin,  # type: ignore[arg-type]
        motive=motive,
        disclosure=disclosure,  # type: ignore[arg-type]
        source_entry_ids=source_entry_ids,
        willingness=willingness,
        outcome=outcome,  # type: ignore[arg-type]
        not_before=not_before,
        expires_at=iso(expires_at),
    )


class AgencyCapacityResult:
    __slots__ = ("allowed", "reason", "next_opportunity_at")

    def __init__(self, allowed: bool, reason: str, next_opportunity_at: datetime | None = None) -> None:
        self.allowed = allowed
        self.reason = reason
        self.next_opportunity_at = next_opportunity_at


def evaluate_agency_capacity(
    window: AgencyWindowState | None,
    candidate: ProactiveContactDraft,
    now: datetime,
    config: AgencyConfig,
    last_character_message_at: str | None = None,
) -> AgencyCapacityResult:
    if window is None:
        return AgencyCapacityResult(False, "agency-window-missing-or-expired")
    valid_until = parse_date(window.valid_until)
    if valid_until is None or valid_until <= now:
        return AgencyCapacityResult(False, "agency-window-missing-or-expired")

    def future(value: str | None) -> datetime | None:
        date = parse_date(value)
        return date if date is not None and date > now else None

    next_opportunity = future(window.next_opportunity_at)
    if window.device_access == "unavailable":
        return AgencyCapacityResult(False, "device-unavailable", next_opportunity)
    if window.device_access == "limited":
        return AgencyCapacityResult(False, "device-limited", next_opportunity)
    if window.activity_load == "overloaded":
        return AgencyCapacityResult(False, "schedule-overloaded", next_opportunity)
    if candidate.disclosure == "personal" and window.privacy != "private":
        return AgencyCapacityResult(False, "privacy-insufficient", next_opportunity)

    last_contact = parse_date(last_character_message_at)
    minimum_interval_seconds = max(0, config.minimum_proactive_interval_minutes) * MINUTE
    if (
        candidate.origin != "promise"
        and last_contact is not None
        and (now - last_contact).total_seconds() < minimum_interval_seconds
    ):
        return AgencyCapacityResult(
            False,
            "minimum-proactive-interval",
            last_contact + timedelta(seconds=minimum_interval_seconds),
        )
    if (
        window.activity_load == "occupied"
        and candidate.origin not in ("promise", "practical-update")
    ):
        return AgencyCapacityResult(False, "schedule-occupied", next_opportunity)
    return AgencyCapacityResult(True, "capacity-available")


def proactive_candidate_fingerprint(candidate: ProactiveContactDraft) -> str:
    return "|".join(
        [
            candidate.participant_id,
            candidate.origin,
            ",".join(str(entry_id) for entry_id in sorted(candidate.source_entry_ids)),
        ]
    )


def proactive_recheck_at(
    candidate: ProactiveContactDraft,
    capacity: AgencyCapacityResult,
    window: AgencyWindowState,
    now: datetime,
) -> datetime:
    requested = parse_date(candidate.not_before)
    capacity_time = capacity.next_opportunity_at
    window_time = parse_date(window.next_opportunity_at)
    fallback = now + timedelta(minutes=30)
    options = [value for value in (requested, capacity_time, window_time) if value is not None and value > now]
    selected = min(options) if options else fallback
    expiry = parse_date(candidate.expires_at) or (now + timedelta(hours=1))
    return min(selected, expiry)


def proactive_origin_bypasses_ordinary_interval(origin: str) -> bool:
    return origin == "promise"


# ------------------------------------------------------------- URL safety

def is_private_host(host: str) -> bool:
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        return (
            a == 10
            or a == 127
            or a == 0
            or (a == 169 and b == 254)
            or (a == 172 and 16 <= b <= 31)
            or (a == 192 and b == 168)
        )
    return ":" in host


def normalize_domains(values: list[str] | None) -> list[str]:
    out = []
    for value in values or []:
        text = str(value).strip().lower().strip(".")
        if text:
            out.append(text)
    return out


def domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def is_safe_public_web_url(value: str, blocked_domains: list[str] | None, allowed_domains: list[str] | None) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".localhost") or host == "::1":
        return False
    if is_safe_public_web_url._is_private(host):  # type: ignore[attr-defined]
        return False
    blocked = normalize_domains(blocked_domains)
    allowed = normalize_domains(allowed_domains)
    if any(domain_matches(host, domain) for domain in blocked):
        return False
    return not allowed or any(domain_matches(host, domain) for domain in allowed)


def _is_private(host: str) -> bool:
    return is_private_host(host)


is_safe_public_web_url._is_private = staticmethod(_is_private)  # type: ignore[attr-defined]
