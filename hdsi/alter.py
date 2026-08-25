"""Alter System pure state logic. Port of HDS-Interlude src/alter.ts.

All thresholds, weights and lifecycle rules keep the original semantics:
- integer score clamped to -5..5
- dynamic threshold from recent one-hour turn density
- weight boost/decay with minWeight cleanup
- completion zeroes accumulation and installs a bounded offset
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .types import (
    AlterHistoryEntry,
    AlterSystemState,
    EmotionalOffset,
    EmotionalOffsetPrompt,
    NarrativePhase,
    clamp_number,
    iso,
    parse_date,
)

HOUR_MS = 60 * 60 * 1000
HISTORY_LIMIT = 50

DEFAULT_ALTER_CONFIG: dict[str, Any] = {
    "enabled": False,
    "base_threshold": 10,
    "density_factor": 0.3,
    "same_direction_boost": 0.05,
    "opposite_decay": 0.15,
    "min_weight": 0.2,
    "max_intensity": 2.0,
    "model_slot": "",
    "temperature": 0.3,
    "top_p": 1.0,
    "max_tokens": 400,
    "timeout": 30_000,
    "prompt": "",
}


def resolve_alter_config(value: Optional[dict[str, Any]] | None) -> dict[str, Any]:
    config = dict(DEFAULT_ALTER_CONFIG)
    if isinstance(value, dict):
        for key in config:
            if key in value:
                config[key] = value[key]
        # Accept legacy TS-style keys as well.
        alias = {
            "baseThreshold": "base_threshold",
            "densityFactor": "density_factor",
            "sameDirectionBoost": "same_direction_boost",
            "oppositeDecay": "opposite_decay",
            "minWeight": "min_weight",
            "maxIntensity": "max_intensity",
            "modelId": "model_slot",
        }
        for old, new in alias.items():
            if old in value:
                config[new] = value[old]
    return config


def normalize_alter_value(value: Any) -> Optional[int]:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    if not math.isfinite(value):
        return None
    return max(-5, min(5, round(value)))


def create_alter_system_state(now: datetime | None = None) -> AlterSystemState:
    return AlterSystemState(
        alter_value=0,
        alter_weight=0,
        last_trigger_direction=0,
        emotional_offset=None,
        history=[],
        last_updated_at=iso(now or datetime.now(timezone.utc)),
    )


def _finite_number(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if math.isfinite(value) else fallback
    return fallback


def normalize_alter_system_state(value: Any) -> Optional[AlterSystemState]:
    """Read-time normalization including the legacy lastTriggerAlter field."""
    if isinstance(value, AlterSystemState):
        return value
    if not isinstance(value, dict):
        return None

    history_raw = value.get("history")
    history: list[AlterHistoryEntry] = []
    if isinstance(history_raw, list):
        for index, entry in enumerate(history_raw):
            if not isinstance(entry, dict):
                continue
            phase = entry.get("phase")
            phase_value = phase if phase in NarrativePhase._value2member_map_ else "user-message"
            history.append(
                AlterHistoryEntry(
                    turn=max(1, math.floor(_finite_number(entry.get("turn"), index + 1))),
                    phase=phase_value,
                    alter=normalize_alter_value(entry.get("alter")) or 0,
                    alter_value=clamp_number(entry.get("alterValue", entry.get("alter_value")), 0, -1000, 1000),
                    timestamp=iso(parse_date(entry.get("timestamp"))),
                )
            )
    history = history[-HISTORY_LIMIT:]

    offset_raw = value.get("emotionalOffset", value.get("emotional_offset"))
    emotional_offset: Optional[EmotionalOffset] = None
    if isinstance(offset_raw, dict) and isinstance(offset_raw.get("description"), str):
        direction = "relaxed" if offset_raw.get("direction") == "relaxed" else "serious"
        emotional_offset = EmotionalOffset(
            direction=direction,  # type: ignore[arg-type]
            description=offset_raw["description"].strip()[:800],
            intensity=clamp_number(offset_raw.get("intensity"), 1, 0, 3),
            generated_at=iso(parse_date(offset_raw.get("generatedAt", offset_raw.get("generated_at")))),
        )

    legacy_direction = _sign(_finite_number(value.get("lastTriggerAlter"), 0))
    raw_direction = value.get("lastTriggerDirection")
    if not isinstance(raw_direction, (int, float)) or isinstance(raw_direction, bool):
        raw_direction = legacy_direction
    direction = _sign(_finite_number(raw_direction, legacy_direction))

    return AlterSystemState(
        alter_value=clamp_number(value.get("alterValue", value.get("alter_value")), 0, -1000, 1000),
        alter_weight=clamp_number(value.get("alterWeight", value.get("alter_weight")), 0, 0, 1),
        last_trigger_direction=direction,
        emotional_offset=emotional_offset,
        history=history,
        last_updated_at=iso(parse_date(value.get("lastUpdatedAt", value.get("last_updated_at")))),
        last_analysis_attempt_at=_opt_iso(value, "lastAnalysisAttemptAt", "last_analysis_attempt_at"),
    )


def _opt_iso(record: dict[str, Any], key_ts: str, key_py: str | None = None) -> Optional[str]:
    keys = [key_ts] + ([key_py] if key_py and key_py != key_ts else [])
    for key in keys:
        if key in record:
            parsed = parse_date(record.get(key))
            if parsed is not None:
                return iso(parsed)
    return None


def _sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def calculate_alter_threshold(
    history: list[AlterHistoryEntry],
    config: dict[str, Any],
    now: datetime | None = None,
) -> float:
    now = now or datetime.now(timezone.utc)
    one_hour_ago_ms = (now - timedelta(hours=1)).timestamp() * 1000
    turns = 0
    for entry in history:
        ts = parse_date(entry.timestamp)
        if ts is not None and ts.timestamp() * 1000 >= one_hour_ago_ms:
            turns += 1
    density = min(turns / 10, 1)
    base = max(1.0, _finite_number(config.get("base_threshold"), 10))
    factor = clamp_number(config.get("density_factor"), 0.3, 0, 1)
    return max(base * 0.5, base * (1 - density * factor))


def adjust_alter_weight(
    weight: float,
    same_direction: bool,
    magnitude: float,
    config: dict[str, Any],
) -> float:
    rate = config.get("same_direction_boost") if same_direction else -config.get("opposite_decay")
    rate = _finite_number(rate, 0)
    return clamp_number(weight + max(0.0, magnitude) * rate, 0, 0, 1)


class AlterTurnResult:
    __slots__ = ("state", "threshold", "offset_expired", "threshold_reached")

    def __init__(
        self,
        state: AlterSystemState,
        threshold: float,
        offset_expired: bool,
        threshold_reached: bool,
    ) -> None:
        self.state = state
        self.threshold = threshold
        self.offset_expired = offset_expired
        self.threshold_reached = threshold_reached


def advance_alter_system(
    current: AlterSystemState | None,
    alter: int,
    phase: str,
    now: datetime,
    config: dict[str, Any],
) -> AlterTurnResult:
    state = current.model_copy(deep=True) if current is not None else create_alter_system_state(now)
    state.alter_value = clamp_number(state.alter_value + alter, 0, -1000, 1000)
    direction = _sign(alter)
    offset_expired = False
    if state.emotional_offset is not None and direction:
        state.alter_weight = adjust_alter_weight(
            state.alter_weight,
            direction == state.last_trigger_direction,
            abs(alter),
            config,
        )
        if state.alter_weight < config.get("min_weight", 0.2):
            state.emotional_offset = None
            state.alter_weight = 0
            offset_expired = True
    state.history.append(
        AlterHistoryEntry(
            turn=(state.history[-1].turn if state.history else 0) + 1,
            phase=phase if phase in NarrativePhase._value2member_map_ else "user-message",
            alter=alter,
            alter_value=state.alter_value,
            timestamp=iso(now),
        )
    )
    state.history = state.history[-HISTORY_LIMIT:]
    state.last_updated_at = iso(now)
    threshold = calculate_alter_threshold(state.history, config, now)
    return AlterTurnResult(
        state=state,
        threshold=threshold,
        offset_expired=offset_expired,
        threshold_reached=abs(state.alter_value) >= threshold,
    )


def complete_alter_analysis(
    state: AlterSystemState,
    description: str,
    threshold: float,
    now: datetime,
    config: dict[str, Any],
) -> AlterSystemState:
    trigger_value = state.alter_value
    direction_int = _sign(trigger_value)
    direction: Literal_serious = "serious" if direction_int > 0 else "relaxed"
    intensity = min(
        abs(trigger_value) / max(1.0, threshold),
        _finite_number(config.get("max_intensity"), 2),
    )
    state.emotional_offset = EmotionalOffset(
        direction=direction,  # type: ignore[arg-type]
        description=description.strip()[:800],
        intensity=intensity,
        generated_at=iso(now),
    )
    state.alter_value = 0
    state.alter_weight = 1
    state.last_trigger_direction = direction_int if direction_int else state.last_trigger_direction
    state.last_updated_at = iso(now)
    return state


Literal_serious = str  # annotation helper alias to keep pydantic out of this module


def emotional_offset_for_prompt(
    state: AlterSystemState | None,
    config: dict[str, Any],
) -> Optional[EmotionalOffsetPrompt]:
    if not config.get("enabled") or state is None or state.emotional_offset is None:
        return None
    if state.alter_weight < config.get("min_weight", 0.2):
        return None
    offset = state.emotional_offset
    return EmotionalOffsetPrompt(
        direction=offset.direction,  # type: ignore[arg-type]
        description=offset.description,
        intensity=offset.intensity,
        generated_at=offset.generated_at,
        weight=state.alter_weight,
    )


def alter_analysis_cooling_down(
    state: AlterSystemState,
    now: datetime | None = None,
    cooldown_seconds: int = 5 * 60,
) -> bool:
    now = now or datetime.now(timezone.utc)
    last_attempt = parse_date(state.last_analysis_attempt_at)
    if last_attempt is None:
        return False
    return (now - last_attempt).total_seconds() < cooldown_seconds
