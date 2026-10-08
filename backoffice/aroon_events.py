#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import statistics
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Optional


PAIRS = [
    "USD/CHF",
    "USD/EUR",
    "USD/JPY",
    "EUR/CHF",
    "EUR/JPY",
    "CHF/JPY",
]

# Horizon names describe intended observation scales, not exact calendar spans.
HORIZONS_BY_GRANULARITY = {
    "1D": {"5d": 5, "5w": 25, "5m": 105},
    "1W": {"5w": 5, "5m": 21},
    "1M": {"5m": 5, "1y": 12},
    "1Q": {"1y": 4, "2y": 8},
}

GRANULARITIES = ("1D", "1W", "1M", "1Q")

BARRIER_EPSILON_PCT = 1e-10


@dataclass
class AroonPoint:
    up: Optional[float]
    down: Optional[float]
    separation: Optional[float]


@dataclass
class CandlePoint:
    start_date: str
    end_date: str
    open: float
    high: float
    low: float
    close: float
    observations: int

def load_snapshot(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload["rows"] if isinstance(payload, dict) and "rows" in payload else payload

    if not isinstance(rows, list):
        raise ValueError("Snapshot must be a row list or contain top-level 'rows'.")

    required = {"date", "EUR", "USD", "CHF", "JPY"}
    cleaned = []

    for row in rows:
        if not required.issubset(row):
            continue
        cleaned.append(
            {
                "date": str(row["date"]),
                "EUR": float(row["EUR"]),
                "USD": float(row["USD"]),
                "CHF": float(row["CHF"]),
                "JPY": float(row["JPY"]),
            }
        )

    cleaned.sort(key=lambda r: r["date"])

    if not cleaned:
        raise ValueError("No usable synchronized observations found.")

    return cleaned


def pair_series(rows: list[dict], pair: str) -> list[tuple[str, float]]:
    base, quote = pair.split("/")
    return [(row["date"], row[quote] / row[base]) for row in rows]

def to_candles(
    series: list[tuple[str, float]],
    granularity: str,
) -> list[CandlePoint]:
    if granularity == "1D":
        return [
            CandlePoint(
                start_date=d,
                end_date=d,
                open=value,
                high=value,
                low=value,
                close=value,
                observations=1,
            )
            for d, value in series
        ]

    buckets: dict[tuple, list[tuple[str, float]]] = {}

    for d, value in series:
        parsed = date.fromisoformat(d)

        if granularity == "1W":
            iso_year, iso_week, _ = parsed.isocalendar()
            key = (iso_year, iso_week)

        elif granularity == "1M":
            key = (parsed.year, parsed.month)

        elif granularity == "1Q":
            quarter = (parsed.month - 1) // 3 + 1
            key = (parsed.year, quarter)

        else:
            raise ValueError(
                f"Unsupported granularity: {granularity}"
            )

        buckets.setdefault(key, []).append((d, value))

    candles = []

    for key in sorted(buckets):
        rows = buckets[key]
        values = [value for _, value in rows]

        candles.append(
            CandlePoint(
                start_date=rows[0][0],
                end_date=rows[-1][0],
                open=rows[0][1],
                high=max(values),
                low=min(values),
                close=rows[-1][1],
                observations=len(rows),
            )
        )

    return candles

def signal_series_from_candles(
    candles: list[CandlePoint],
) -> list[tuple[str, float]]:
    return [
        (candle.end_date, candle.close)
        for candle in candles
    ]


def aroon(values: list[float], period: int) -> list[AroonPoint]:
    result: list[AroonPoint] = []

    for i in range(len(values)):
        if i < period - 1:
            result.append(AroonPoint(None, None, None))
            continue

        window = values[i - period + 1 : i + 1]
        hi = max(window)
        lo = min(window)

        if hi == lo:
            result.append(AroonPoint(0.0, 0.0, 0.0))
            continue

        hi_idx = len(window) - 1 - window[::-1].index(hi)
        lo_idx = len(window) - 1 - window[::-1].index(lo)

        up = 100.0 * hi_idx / (period - 1)
        down = 100.0 * lo_idx / (period - 1)
        result.append(AroonPoint(up, down, up - down))

    return result


def stochastic(
    values: list[float],
    period: int,
    smooth: int,
) -> tuple[list[Optional[float]], list[Optional[float]]]:
    k: list[Optional[float]] = []

    for i, value in enumerate(values):
        if i < period - 1:
            k.append(None)
            continue

        window = values[i - period + 1 : i + 1]
        lo = min(window)
        hi = max(window)
        k.append(50.0 if hi == lo else 100.0 * (value - lo) / (hi - lo))

    d: list[Optional[float]] = []

    for i in range(len(k)):
        if i < smooth - 1:
            d.append(None)
            continue

        window = k[i - smooth + 1 : i + 1]
        if any(v is None for v in window):
            d.append(None)
            continue

        vals = [float(v) for v in window if v is not None]
        d.append(sum(vals) / len(vals))

    return k, d


def transition_cause(
    values: list[float],
    index: int,
    period: int,
    state: str,
) -> str:
    """
    Distinguish a fresh directional extreme from a transition that can occur
    because an older opposite extreme drops out of the rolling window.
    """
    if index < period - 1:
        return "WARMUP"

    window = values[index - period + 1 : index + 1]
    current = values[index]

    if state == "BULL_TRANSITION" and current == max(window):
        return "FRESH_DIRECTIONAL_EXTREME"

    if state == "BEAR_TRANSITION" and current == min(window):
        return "FRESH_DIRECTIONAL_EXTREME"

    return "WINDOW_TURNOVER_OR_OTHER"


def classify_state(
    previous: AroonPoint,
    current: AroonPoint,
    *,
    min_separation: float,
    parallel_tolerance: float,
) -> tuple[str, str, str]:
    """
    Returns (directional_state, event_action, reason).

    Direction and entry action are separate:
    - BULLISH / BEARISH / WEAK_OR_FLAT describes the current state.
    - NEW_TRANSITION / NO_NEW_ENTRY describes whether a new crossover event exists.
    """

    if (
        previous.up is None
        or previous.down is None
        or previous.separation is None
        or current.up is None
        or current.down is None
        or current.separation is None
    ):
        return "WARMUP", "NO_NEW_ENTRY", "WARMUP"

    up_change = current.up - previous.up
    down_change = current.down - previous.down
    sep_change = current.separation - previous.separation

    if current.separation >= min_separation:
        direction = "BULLISH"
    elif current.separation <= -min_separation:
        direction = "BEARISH"
    else:
        direction = "WEAK_OR_FLAT"

    # Transition = sign crossover into sufficient separation, with opposing line movement.
    if (
        previous.separation <= 0
        and current.separation >= min_separation
        and up_change > 0
        and down_change < 0
    ):
        return direction, "NEW_TRANSITION", "BULL_TRANSITION"

    if (
        previous.separation >= 0
        and current.separation <= -min_separation
        and up_change < 0
        and down_change > 0
    ):
        return direction, "NEW_TRANSITION", "BEAR_TRANSITION"

    if direction == "WEAK_OR_FLAT":
        return direction, "NO_NEW_ENTRY", "WEAK_SEPARATION"

    if abs(sep_change) <= parallel_tolerance:
        return direction, "NO_NEW_ENTRY", "STABLE_OR_PARALLEL_EXTREME_STATE"

    return direction, "NO_NEW_ENTRY", "ESTABLISHED_DIRECTION"


def stochastic_confirmation(
    reason: str,
    k_prev: Optional[float],
    d_prev: Optional[float],
    k_now: Optional[float],
    d_now: Optional[float],
) -> str:
    if reason not in {"BULL_TRANSITION", "BEAR_TRANSITION"}:
        return "NOT_APPLICABLE"

    if None in (k_prev, d_prev, k_now, d_now):
        return "UNAVAILABLE"

    assert k_prev is not None and d_prev is not None
    assert k_now is not None and d_now is not None

    bull_cross = k_prev <= d_prev and k_now > d_now
    bear_cross = k_prev >= d_prev and k_now < d_now

    if reason == "BULL_TRANSITION":
        return "CONFIRMS" if bull_cross else "DOES_NOT_CONFIRM"

    return "CONFIRMS" if bear_cross else "DOES_NOT_CONFIRM"


def pct_move(entry: float, value: float, direction: int) -> float:
    return direction * 100.0 * (value / entry - 1.0)


def barrier_ge(value: float, target: float) -> bool:
    return value + BARRIER_EPSILON_PCT >= target


def barrier_le(value: float, target: float) -> bool:
    return value - BARRIER_EPSILON_PCT <= target

def transition_shape(row: dict) -> str:
    cause = row.get("transition_cause")
    separation_change = row.get("separation_change")

    if cause == "FRESH_DIRECTIONAL_EXTREME":
        return "FRESH_EXTREME"

    if cause == "WINDOW_TURNOVER_OR_OTHER":
        if (
            separation_change is not None
            and abs(separation_change) >= 50.0
        ):
            return "TURNOVER_STRONG_EXPANSION"

        return "WINDOW_TURNOVER_OR_OTHER"

    return "OTHER_TRANSITION"

def evaluate_path(
    *,
    entry: float,
    path: list[tuple[str, float]],
    direction: int,
    target_pct: float,
    complete: bool,
    endpoint_date: Optional[str],
    elapsed_days: Optional[int],
) -> dict:
    if not path:
        return {
            "available": False,
            "complete": complete,
            "censored": not complete,
            "observations": 0,
            "endpoint_date": endpoint_date,
            "elapsed_days": elapsed_days,
        }

    returns = [pct_move(entry, value, direction) for _, value in path]

    # Include entry baseline, so an all-losing path has MFE=0 and an
    # all-winning path has MAE=0.
    mfe = max([0.0] + returns)
    mae = min([0.0] + returns)

    target_step = None
    adverse_step = None
    target_date = None
    adverse_date = None

    for offset, ((d, _), move) in enumerate(zip(path, returns), start=1):
        if target_step is None and barrier_ge(move, target_pct):
            target_step = offset
            target_date = d

        if adverse_step is None and barrier_le(move, -target_pct):
            adverse_step = offset
            adverse_date = d

    if target_step is not None and adverse_step is not None:
        if target_step < adverse_step:
            outcome = "TARGET_FIRST"
        elif adverse_step < target_step:
            outcome = "ADVERSE_FIRST"
        else:
            outcome = "SAME_OBSERVATION_BOTH"
    elif target_step is not None:
        outcome = "TARGET_ONLY"
    elif adverse_step is not None:
        outcome = "ADVERSE_ONLY"
    else:
        outcome = "NEITHER"

    return {
        "available": True,
        "complete": complete,
        "censored": not complete,
        "observations": len(path),
        "endpoint_date": endpoint_date,
        "elapsed_days": elapsed_days,
        "directional_endpoint_return_pct": returns[-1],
        "max_favorable_excursion_pct": mfe,
        "max_adverse_excursion_pct": mae,
        "target_pct": target_pct,
        "target_hit": target_step is not None,
        "target_hit_after_observations": target_step,
        "target_hit_date": target_date,
        "adverse_hit": adverse_step is not None,
        "adverse_hit_after_observations": adverse_step,
        "adverse_hit_date": adverse_date,
        "barrier_outcome": outcome,
    }

def evidence_label(forward: dict) -> str:
    if not forward.get("available", False):
        return "CENSORED"

    if not forward.get("complete", False):
        return "CENSORED"

    outcome = forward["barrier_outcome"]

    if outcome in {"TARGET_FIRST", "TARGET_ONLY"}:
        return "SUPPORTED"

    if outcome in {"ADVERSE_FIRST", "ADVERSE_ONLY"}:
        return "ADVERSE_FIRST"

    if outcome == "SAME_OBSERVATION_BOTH":
        return "AMBIGUOUS"

    if outcome == "NEITHER":
        return "UNRESOLVED"

    raise ValueError(f"Unknown barrier outcome: {outcome}")

def daily_forward_metrics(
    series: list[tuple[str, float]],
    index: int,
    *,
    direction: int,
    target_pct: float,
) -> dict:
    entry_date, entry = series[index]
    result = {}

    for name, steps in HORIZONS_BY_GRANULARITY["1D"].items():
        needed_end = index + steps
        complete = needed_end < len(series)
        actual_end = min(needed_end, len(series) - 1)

        if actual_end <= index:
            result[name] = evaluate_path(
                entry=entry,
                path=[],
                direction=direction,
                target_pct=target_pct,
                complete=False,
                endpoint_date=None,
                elapsed_days=None,
            )
            continue

        path = series[index + 1 : actual_end + 1]
        endpoint_date = series[actual_end][0]
        elapsed_days = (
            date.fromisoformat(endpoint_date) - date.fromisoformat(entry_date)
        ).days

        result[name] = evaluate_path(
            entry=entry,
            path=path,
            direction=direction,
            target_pct=target_pct,
            complete=complete,
            endpoint_date=endpoint_date,
            elapsed_days=elapsed_days,
        )

    return result

def coarse_forward_metrics(
    signal_series: list[tuple[str, float]],
    signal_index: int,
    daily_series: list[tuple[str, float]],
    *,
    granularity: str,
    direction: int,
    target_pct: float,
) -> dict:
    """
    Generate the signal on coarse candle closes, but evaluate excursions
    using every intervening daily ECB reference observation.
    """
    entry_date, entry = signal_series[signal_index]
    result = {}

    horizons = HORIZONS_BY_GRANULARITY[granularity]

    for name, steps in horizons.items():
        needed_index = signal_index + steps
        complete = needed_index < len(signal_series)

        if complete:
            endpoint_date = signal_series[needed_index][0]
        else:
            endpoint_date = signal_series[-1][0]

        path = [
            (d, value)
            for d, value in daily_series
            if entry_date < d <= endpoint_date
        ]

        elapsed_days = (
            date.fromisoformat(endpoint_date)
            - date.fromisoformat(entry_date)
        ).days

        result[name] = evaluate_path(
            entry=entry,
            path=path,
            direction=direction,
            target_pct=target_pct,
            complete=complete,
            endpoint_date=endpoint_date,
            elapsed_days=elapsed_days,
        )

    return result


def build_ledger(
    daily_series: list[tuple[str, float]],
    *,
    signal_series: list[tuple[str, float]],
    pair: str,
    granularity: str,
    aroon_period: int,
    stoch_period: int,
    stoch_smooth: int,
    min_separation: float,
    parallel_tolerance: float,
    target_pct: float,
) -> list[dict]:
    signal_dates = [d for d, _ in signal_series]
    signal_values = [v for _, v in signal_series]

    ar = aroon(signal_values, aroon_period)
    stoch_k, stoch_d = stochastic(signal_values, stoch_period, stoch_smooth)

    ledger = []

    for i in range(1, len(signal_values)):
        direction_state, event_action, reason = classify_state(
            ar[i - 1],
            ar[i],
            min_separation=min_separation,
            parallel_tolerance=parallel_tolerance,
        )

        if direction_state == "WARMUP":
            continue

        current = ar[i]
        previous = ar[i - 1]

        assert current.up is not None
        assert current.down is not None
        assert current.separation is not None
        assert previous.up is not None
        assert previous.down is not None
        assert previous.separation is not None

        up_change = current.up - previous.up
        down_change = current.down - previous.down
        separation_change = current.separation - previous.separation

        confirmation = stochastic_confirmation(
            reason,
            stoch_k[i - 1],
            stoch_d[i - 1],
            stoch_k[i],
            stoch_d[i],
        )

        if reason == "BULL_TRANSITION":
            direction = 1
        elif reason == "BEAR_TRANSITION":
            direction = -1
        else:
            direction = 0

        cause = (
            transition_cause(signal_values, i, aroon_period, reason)
            if event_action == "NEW_TRANSITION"
            else "NOT_APPLICABLE"
        )

        if direction != 0:
            if granularity == "1D":
                forward = daily_forward_metrics(
                    daily_series,
                    i,
                    direction=direction,
                    target_pct=target_pct,
                )
            else:
                forward = coarse_forward_metrics(
                    signal_series,
                    i,
                    daily_series,
                    granularity=granularity,
                    direction=direction,
                    target_pct=target_pct,
                )
        else:
            forward = {}

        row = {
                "pair": pair,
                "granularity": granularity,
                "signal_date": signal_dates[i],
                "signal_index": i,
                "entry_convention": "same-reference-price-anchor_descriptive_only",
                "directional_state": direction_state,
                "event_action": event_action,
                "reason": reason,
                "transition_cause": cause,
                "aroon_period": aroon_period,
                "aroon_up": current.up,
                "aroon_down": current.down,
                "separation": current.separation,
                "separation_change": separation_change,
                "up_change": up_change,
                "down_change": down_change,
                "stochastic_k": stoch_k[i],
                "stochastic_d": stoch_d[i],
                "stochastic_confirmation": confirmation,
                "forward": forward,
            }
        row["transition_shape"] = transition_shape(row)

        ledger.append(row)

    return ledger


def quantile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)

    if lo == hi:
        return ordered[lo]

    frac = pos - lo
    return ordered[lo] * (1 - frac) + ordered[hi] * frac


def summarize_transition_group(events: list[dict], horizon: str) -> dict:
    rows = [
        event["forward"][horizon]
        for event in events
        if horizon in event["forward"]
    ]

    complete = [row for row in rows if row.get("available") and row.get("complete")]
    censored = [row for row in rows if row.get("available") and not row.get("complete")]

    outcomes = {
        "TARGET_FIRST": 0,
        "ADVERSE_FIRST": 0,
        "TARGET_ONLY": 0,
        "ADVERSE_ONLY": 0,
        "SAME_OBSERVATION_BOTH": 0,
        "NEITHER": 0,
    }

    evidence = {
        "SUPPORTED": 0,
        "ADVERSE_FIRST": 0,
        "UNRESOLVED": 0,
        "AMBIGUOUS": 0,
        "CENSORED": 0,
    }

    for row in complete:
        outcomes[row["barrier_outcome"]] += 1
        evidence[evidence_label(row)] += 1

    for row in censored:
        evidence[evidence_label(row)] += 1

    target_before_adverse = (
        outcomes["TARGET_FIRST"] + outcomes["TARGET_ONLY"]
    )

    target_hit = sum(
        1 for row in complete if row["target_hit"]
    )

    mfes = [row["max_favorable_excursion_pct"] for row in complete]
    maes = [row["max_adverse_excursion_pct"] for row in complete]
    endpoints = [row["directional_endpoint_return_pct"] for row in complete]

    shape_counts: dict[str, dict[str, int]] = {}

    for event in events:
        if horizon not in event["forward"]:
            continue

        forward = event["forward"][horizon]
        shape = event.get("transition_shape", "UNKNOWN")
        label = evidence_label(forward)

        bucket = shape_counts.setdefault(
            shape,
            {
                "SUPPORTED": 0,
                "ADVERSE_FIRST": 0,
                "UNRESOLVED": 0,
                "AMBIGUOUS": 0,
                "CENSORED": 0,
            },
        )

        bucket[label] += 1

    return {
        "complete_samples": len(complete),
        "censored_samples": len(censored),
        "evidence": evidence,
        "target_hit_rate": (
            target_hit / len(complete) if complete else None
        ),
        "target_before_adverse_rate": (
            target_before_adverse / len(complete) if complete else None
        ),
        "barrier_outcomes": outcomes,
        "mfe": {
            "mean": statistics.fmean(mfes) if mfes else None,
            "median": statistics.median(mfes) if mfes else None,
            "p90": quantile(mfes, 0.90),
        },
        "mae": {
            "mean": statistics.fmean(maes) if maes else None,
            "median": statistics.median(maes) if maes else None,
            "p10": quantile(maes, 0.10),
        },
        "endpoint_return": {
            "mean": statistics.fmean(endpoints) if endpoints else None,
            "median": statistics.median(endpoints) if endpoints else None,
        },
        "transition_shapes": shape_counts,
    }


def summarize(ledger: list[dict], granularity: str) -> dict:
    transitions = [
        row for row in ledger if row["event_action"] == "NEW_TRANSITION"
    ]
    bull = [row for row in transitions if row["reason"] == "BULL_TRANSITION"]
    bear = [row for row in transitions if row["reason"] == "BEAR_TRANSITION"]

    confirmed = [
        row for row in transitions
        if row["stochastic_confirmation"] == "CONFIRMS"
    ]
    unconfirmed = [
        row for row in transitions
        if row["stochastic_confirmation"] == "DOES_NOT_CONFIRM"
    ]
    unavailable = [
        row for row in transitions
        if row["stochastic_confirmation"] == "UNAVAILABLE"
    ]

    state_counts = {}
    reason_counts = {}

    for row in ledger:
        state_counts[row["directional_state"]] = (
            state_counts.get(row["directional_state"], 0) + 1
        )
        reason_counts[row["reason"]] = (
            reason_counts.get(row["reason"], 0) + 1
        )

    horizons = HORIZONS_BY_GRANULARITY[granularity].keys()

    horizon_summary = {}

    for horizon in horizons:
        horizon_summary[horizon] = {
            "all_transitions": summarize_transition_group(
                transitions, horizon
            ),
            "bull_transitions": summarize_transition_group(
                bull, horizon
            ),
            "bear_transitions": summarize_transition_group(
                bear, horizon
            ),
            "stochastic_confirmed": summarize_transition_group(
                confirmed, horizon
            ),
            "stochastic_unconfirmed": summarize_transition_group(
                unconfirmed, horizon
            ),
            "stochastic_unavailable": summarize_transition_group(
                unavailable, horizon
            ),
        }

    return {
        "ledger_observations": len(ledger),
        "state_counts": state_counts,
        "reason_counts": reason_counts,
        "transition_count": len(transitions),
        "bull_transition_count": len(bull),
        "bear_transition_count": len(bear),
        "stochastic_confirmation": {
            "confirmed": len(confirmed),
            "unconfirmed": len(unconfirmed),
            "unavailable": len(unavailable),
        },
        "transition_causes": {
            "fresh_directional_extreme": sum(
                row["transition_cause"] == "FRESH_DIRECTIONAL_EXTREME"
                for row in transitions
            ),
            "window_turnover_or_other": sum(
                row["transition_cause"] == "WINDOW_TURNOVER_OR_OTHER"
                for row in transitions
            ),
        },
        "horizons": horizon_summary,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fixed-parameter Aroon transition diagnostics using an archived "
            "ECB reference-rate snapshot."
        )
    )

    parser.add_argument(
        "--snapshot",
        default="backoffice/artifacts/ecb_daily_2025-12-31.json",
    )
    parser.add_argument("--pair", choices=PAIRS, default="USD/CHF")
    parser.add_argument(
        "--granularity",
        choices=GRANULARITIES,
        default="1D",
    )
    parser.add_argument("--aroon-period", type=int, default=14)
    parser.add_argument("--stoch-period", type=int, default=14)
    parser.add_argument("--stoch-smooth", type=int, default=3)
    parser.add_argument("--min-separation", type=float, default=20.0)
    parser.add_argument("--parallel-tolerance", type=float, default=5.0)
    parser.add_argument("--target-pct", type=float, default=0.5)
    parser.add_argument("--output", default=None)

    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.aroon_period < 2:
        raise ValueError("--aroon-period must be at least 2.")
    if args.stoch_period < 2:
        raise ValueError("--stoch-period must be at least 2.")
    if args.stoch_smooth < 1:
        raise ValueError("--stoch-smooth must be at least 1.")
    if not 0 <= args.min_separation <= 100:
        raise ValueError("--min-separation must be between 0 and 100.")
    if args.parallel_tolerance < 0:
        raise ValueError("--parallel-tolerance must be non-negative.")
    if args.target_pct <= 0:
        raise ValueError("--target-pct must be positive.")


def main() -> int:
    args = parse_args()
    validate_args(args)

    snapshot = Path(args.snapshot)
    rows = load_snapshot(snapshot)
    daily_series = pair_series(rows, args.pair)

    candles = to_candles(
        daily_series,
        args.granularity,
    )

    signal_series = signal_series_from_candles(candles)

    ledger = build_ledger(
        daily_series,
        signal_series=signal_series,
        pair=args.pair,
        granularity=args.granularity,
        aroon_period=args.aroon_period,
        stoch_period=args.stoch_period,
        stoch_smooth=args.stoch_smooth,
        min_separation=args.min_separation,
        parallel_tolerance=args.parallel_tolerance,
        target_pct=args.target_pct,
    )

    payload = {
        "kind": "fx_rotation_lab.aroon_events.v2",
        "source_snapshot": str(snapshot),
        "pair": args.pair,
        "granularity": args.granularity,
        "parameters": {
            "aroon_period": args.aroon_period,
            "stoch_period": args.stoch_period,
            "stoch_smooth": args.stoch_smooth,
            "min_separation": args.min_separation,
            "parallel_tolerance": args.parallel_tolerance,
            "target_pct": args.target_pct,
            "barrier_epsilon_pct": BARRIER_EPSILON_PCT,
        },
        "entry_convention": (
            "Event reference price is a descriptive measurement anchor, "
            "not an executable fill."
        ),
        "coarse_evaluation_resolution": (
            "Coarse signals are generated from aggregated ECB reference-rate "
            "candles, while excursions are evaluated on intervening daily "
            "reference observations through the coarse horizon endpoint."
        ),
        "summary": summarize(ledger, args.granularity),
        "ledger": ledger,
    }

    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {output}")

    compact_horizons = {}

    for horizon, horizon_summary in payload["summary"]["horizons"].items():
        compact_horizons[horizon] = (
            horizon_summary["all_transitions"]["evidence"]
        )

    print(
        json.dumps(
            {
                "pair": payload["pair"],
                "granularity": payload["granularity"],
                "parameters": payload["parameters"],
                "transition_count": payload["summary"]["transition_count"],
                "horizons": compact_horizons,
            },
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
