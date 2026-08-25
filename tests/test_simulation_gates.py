"""Long-run simulation gates for v1.0.0 acceptance.

Full-fidelity runs live in tests/simulation.py (--days N). These pytest
wrappers assert the long-horizon invariants at reduced durations so the suite
stays fast; the 30/90-day runs are exercised via the CLI in release checks.
"""

from __future__ import annotations

import pytest

from tests.simulation import run_simulation


@pytest.mark.asyncio
async def test_simulation_7_days_no_state_corruption():
    report = await run_simulation(days=7, seed=7)
    assert report["passed"], report
    assert report["narrator_turns"] > 100, "simulation should exercise real traffic"


@pytest.mark.asyncio
async def test_simulation_30_days_memory_bounded():
    report = await run_simulation(days=30, seed=11)
    assert report["passed"], report
    details = report["details"]
    # Long-horizon bounds: memory must not balloon.
    assert details["memories"] <= 1500
    assert details["pending_intents"] <= 50
