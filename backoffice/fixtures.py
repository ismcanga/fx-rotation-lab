#!/usr/bin/env python3

from __future__ import annotations

import math
import aroon_events as m
import calibrate

def close(a, b, eps=1e-9):
    assert abs(a - b) <= eps, (a, b)


def test_aroon_flat():
    points = m.aroon([2, 2, 2, 2, 2], 5)
    assert points[-1].up == 0
    assert points[-1].down == 0
    assert points[-1].separation == 0


def test_aroon_recent_tied_extreme():
    points = m.aroon([1, 3, 2, 3, 2], 5)
    close(points[-1].up, 75.0)
    close(points[-1].down, 0.0)


def test_barrier_exact_touch_bull_and_bear():
    bull = m.evaluate_path(
        entry=100.0,
        path=[("2025-01-02", 100.5)],
        direction=1,
        target_pct=0.5,
        complete=True,
        endpoint_date="2025-01-02",
        elapsed_days=1,
    )
    assert bull["target_hit"]

    bear = m.evaluate_path(
        entry=100.0,
        path=[("2025-01-02", 99.5)],
        direction=-1,
        target_pct=0.5,
        complete=True,
        endpoint_date="2025-01-02",
        elapsed_days=1,
    )
    assert bear["target_hit"]


def test_zero_baseline_excursions():
    losing = m.evaluate_path(
        entry=100.0,
        path=[
            ("2025-01-02", 99.9),
            ("2025-01-03", 99.8),
        ],
        direction=1,
        target_pct=0.5,
        complete=True,
        endpoint_date="2025-01-03",
        elapsed_days=2,
    )
    assert losing["max_favorable_excursion_pct"] == 0.0
    assert losing["max_adverse_excursion_pct"] < 0

    winning = m.evaluate_path(
        entry=100.0,
        path=[
            ("2025-01-02", 100.1),
            ("2025-01-03", 100.2),
        ],
        direction=1,
        target_pct=0.5,
        complete=True,
        endpoint_date="2025-01-03",
        elapsed_days=2,
    )
    assert winning["max_adverse_excursion_pct"] == 0.0
    assert winning["max_favorable_excursion_pct"] > 0


def test_barrier_order():
    target_first = m.evaluate_path(
        entry=100.0,
        path=[
            ("2025-01-02", 100.6),
            ("2025-01-03", 99.4),
        ],
        direction=1,
        target_pct=0.5,
        complete=True,
        endpoint_date="2025-01-03",
        elapsed_days=2,
    )
    assert target_first["barrier_outcome"] == "TARGET_FIRST"

    adverse_first = m.evaluate_path(
        entry=100.0,
        path=[
            ("2025-01-02", 99.4),
            ("2025-01-03", 100.6),
        ],
        direction=1,
        target_pct=0.5,
        complete=True,
        endpoint_date="2025-01-03",
        elapsed_days=2,
    )
    assert adverse_first["barrier_outcome"] == "ADVERSE_FIRST"


def test_established_direction_not_no_edge_state():
    previous = m.AroonPoint(up=100.0, down=0.0, separation=100.0)
    current = m.AroonPoint(up=100.0, down=0.0, separation=100.0)

    state, action, reason = m.classify_state(
        previous,
        current,
        min_separation=20.0,
        parallel_tolerance=5.0,
    )

    assert state == "BULLISH"
    assert action == "NO_NEW_ENTRY"
    assert reason == "STABLE_OR_PARALLEL_EXTREME_STATE"


def test_censoring():
    series = [
        ("2025-01-01", 100.0),
        ("2025-01-02", 100.2),
        ("2025-01-03", 100.4),
    ]

    result = m.daily_forward_metrics(
        series,
        1,
        direction=1,
        target_pct=0.5,
    )

    assert result["5d"]["censored"]
    assert not result["5d"]["complete"]


def test_weekly_calendar_completion_for_calibration():
    series = [
        ("2025-12-22", 1.0),
        ("2025-12-24", 1.1),
        ("2025-12-29", 1.2),
        ("2025-12-31", 1.3),
    ]

    candles = m.to_candles(series, "1W")
    assert [c.end_date for c in candles] == ["2025-12-24", "2025-12-31"]
    weekly, metadata = calibrate.research_series(
        series, granularity="1W", as_of="2025-12-31",
    )
    assert weekly == [("2025-12-28", 1.1)]
    assert metadata["excluded_unfinished_candles"] == 1


def main():
    tests = [
        value
        for name, value in globals().items()
        if name.startswith("test_") and callable(value)
    ]

    for test in tests:
        test()
        print(f"PASS {test.__name__}")

    print(f"{len(tests)} fixtures passed")


if __name__ == "__main__":
    main()
