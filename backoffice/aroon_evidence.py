#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path

import aroon_events as ae


PAIRS = ae.PAIRS


def classify_evidence(row: dict, horizon: str) -> str:
    result = row["forward"].get(horizon)

    if not result or not result.get("available"):
        return "UNAVAILABLE"

    if result.get("censored"):
        return "CENSORED"

    outcome = result.get("barrier_outcome")

    if outcome in {"TARGET_FIRST", "TARGET_ONLY"}:
        return "SUPPORTED"

    if outcome in {"ADVERSE_FIRST", "ADVERSE_ONLY"}:
        return "ADVERSE_FIRST"

    if outcome == "SAME_OBSERVATION_BOTH":
        return "AMBIGUOUS"

    if outcome == "NEITHER":
        return "UNRESOLVED"

    return "UNAVAILABLE"


def build_evidence(
    *,
    snapshot: Path,
    pair: str,
    timeframe: str,
    aroon_period: int,
    stoch_period: int,
    stoch_smooth: int,
    min_separation: float,
    parallel_tolerance: float,
    target_pct: float,
    horizon: str,
) -> dict:
    rows = ae.load_snapshot(snapshot)
    daily_series = ae.pair_series(rows, pair)

    ledger = ae.build_ledger(
        daily_series,
        pair=pair,
        timeframe=timeframe,
        aroon_period=aroon_period,
        stoch_period=stoch_period,
        stoch_smooth=stoch_smooth,
        min_separation=min_separation,
        parallel_tolerance=parallel_tolerance,
        target_pct=target_pct,
    )

    transitions = [
        row
        for row in ledger
        if row["event_action"] == "NEW_TRANSITION"
    ]

    markers = []

    for row in transitions:
        result = row["forward"].get(horizon)
        evidence = classify_evidence(row, horizon)

        marker = {
            "signal_date": row["signal_date"],
            "direction": (
                "BULL"
                if row["reason"] == "BULL_TRANSITION"
                else "BEAR"
            ),
            "evidence": evidence,
            "aroon_up": row["aroon_up"],
            "aroon_down": row["aroon_down"],
            "separation": row["separation"],
            "transition_cause": row["transition_cause"],
            "stochastic_confirmation": row["stochastic_confirmation"],
        }

        if result:
            marker["outcome"] = {
                "endpoint_date": result.get("endpoint_date"),
                "elapsed_days": result.get("elapsed_days"),
                "directional_endpoint_return_pct":
                    result.get("directional_endpoint_return_pct"),
                "max_favorable_excursion_pct":
                    result.get("max_favorable_excursion_pct"),
                "max_adverse_excursion_pct":
                    result.get("max_adverse_excursion_pct"),
                "target_hit_date":
                    result.get("target_hit_date"),
                "adverse_hit_date":
                    result.get("adverse_hit_date"),
                "barrier_outcome":
                    result.get("barrier_outcome"),
                "complete":
                    result.get("complete"),
            }

        markers.append(marker)

    counts = {}
    for marker in markers:
        counts[marker["evidence"]] = counts.get(marker["evidence"], 0) + 1

    return {
        "kind": "fx_rotation_lab.aroon_evidence.v1",
        "source_snapshot": str(snapshot),
        "pair": pair,
        "timeframe": timeframe,
        "claim": {
            "indicator": "Aroon",
            "aroon_period": aroon_period,
            "min_separation": min_separation,
            "target_pct": target_pct,
            "horizon": horizon,
        },
        "stochastic": {
            "period": stoch_period,
            "smooth": stoch_smooth,
            "role": "reported separately; not required for Aroon support",
        },
        "price_series": [
            {"date": d, "value": value}
            for d, value in daily_series
        ],
        "evidence_counts": counts,
        "markers": markers,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate temporary evidence markers for one Aroon claim "
            "without modifying the underlying price history."
        )
    )

    parser.add_argument(
        "--snapshot",
        default="backoffice/artifacts/ecb_daily_2025-12-31.json",
    )
    parser.add_argument("--pair", choices=PAIRS, default="USD/CHF")
    parser.add_argument(
        "--timeframe",
        choices=("daily", "weekly"),
        default="daily",
    )
    parser.add_argument("--aroon-period", type=int, default=14)
    parser.add_argument("--stoch-period", type=int, default=14)
    parser.add_argument("--stoch-smooth", type=int, default=3)
    parser.add_argument("--min-separation", type=float, default=20.0)
    parser.add_argument("--parallel-tolerance", type=float, default=5.0)
    parser.add_argument("--target-pct", type=float, default=0.5)
    parser.add_argument(
        "--horizon",
        default="5d",
        help="For daily: 5d, 5w, 5m. For weekly: 5w, 5m.",
    )
    parser.add_argument(
        "--output",
        default="backoffice/artifacts/aroon_evidence.json",
    )

    return parser.parse_args()


def validate_horizon(timeframe: str, horizon: str) -> None:
    allowed = (
        set(ae.DAILY_HORIZONS)
        if timeframe == "daily"
        else set(ae.WEEKLY_HORIZONS)
    )

    if horizon not in allowed:
        raise ValueError(
            f"{timeframe} horizon must be one of: "
            + ", ".join(sorted(allowed))
        )


def main() -> int:
    args = parse_args()
    validate_horizon(args.timeframe, args.horizon)

    payload = build_evidence(
        snapshot=Path(args.snapshot),
        pair=args.pair,
        timeframe=args.timeframe,
        aroon_period=args.aroon_period,
        stoch_period=args.stoch_period,
        stoch_smooth=args.stoch_smooth,
        min_separation=args.min_separation,
        parallel_tolerance=args.parallel_tolerance,
        target_pct=args.target_pct,
        horizon=args.horizon,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote {output}")
    print(json.dumps({
        "pair": payload["pair"],
        "timeframe": payload["timeframe"],
        "claim": payload["claim"],
        "evidence_counts": payload["evidence_counts"],
    }, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
