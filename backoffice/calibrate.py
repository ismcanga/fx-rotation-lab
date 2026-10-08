#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.parse
import urllib.request
from dataclasses import dataclass
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

DEFAULT_START = "2000-01-01"

AROON_MIN = 10
AROON_MAX = 60

FORWARD_HORIZON = 5
VALIDATION_YEARS = 5

PLATEAU_FRACTION = 0.975

# Conservative review gates.
MIN_ABS_CORR_IMPROVEMENT = 0.01
MIN_RELATIVE_IMPROVEMENT = 0.10
MIN_SIGN_CONSISTENCY = 0.80
MAX_WALK_FORWARD_PERIOD_SPAN = 15
MIN_VALIDATION_FOLDS = 4

MIN_SCORE_SAMPLES = 120


@dataclass
class FoldResult:
    validation_year: int
    selected_period: int
    training_correlation: float
    validation_correlation: Optional[float]
    validation_samples: int


def fetch_ecb_rows(start: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "from": start,
            "quotes": "USD,CHF,JPY",
            "providers": "ecb",
        }
    )

    url = f"https://api.frankfurter.dev/v2/rates?{params}"

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fx-rotation-lab-backoffice/2.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)

    by_date: dict[str, dict[str, float]] = {}

    for row in payload:
        row_date = row["date"]
        quote = row["quote"]
        rate = float(row["rate"])

        by_date.setdefault(row_date, {})
        by_date[row_date][quote] = rate

    rows = []

    for row_date in sorted(by_date):
        rates = by_date[row_date]

        if not all(ccy in rates for ccy in ("USD", "CHF", "JPY")):
            continue

        rows.append(
            {
                "date": row_date,
                "EUR": 1.0,
                "USD": rates["USD"],
                "CHF": rates["CHF"],
                "JPY": rates["JPY"],
            }
        )

    if not rows:
        raise RuntimeError("No synchronized ECB observations were returned.")

    return rows


def pair_series(rows: list[dict], pair: str) -> list[tuple[str, float]]:
    base, quote = pair.split("/")
    return [(row["date"], row[quote] / row[base]) for row in rows]


def aroon_oscillator(values: list[float], period: int) -> list[Optional[float]]:
    """
    Returns (Aroon Up - Aroon Down) / 100.

    Project convention:
    - period counts observations
    - current observation is included
    - denominator is period - 1
    - most recent tied extreme wins
    - completely flat window -> oscillator 0
    """

    result: list[Optional[float]] = []

    for i in range(len(values)):
        if i < period - 1:
            result.append(None)
            continue

        window = values[i - period + 1 : i + 1]

        high = max(window)
        low = min(window)

        if high == low:
            result.append(0.0)
            continue

        high_index = len(window) - 1 - window[::-1].index(high)
        low_index = len(window) - 1 - window[::-1].index(low)

        up = high_index / (period - 1)
        down = low_index / (period - 1)

        result.append(up - down)

    return result


def future_log_returns(
    values: list[float],
    horizon: int,
) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)

    for i in range(len(values) - horizon):
        current = values[i]
        future = values[i + horizon]

        if current > 0 and future > 0:
            result[i] = math.log(future / current)

    return result


def pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    if len(xs) != len(ys):
        raise ValueError("Correlation inputs must have equal length.")

    if len(xs) < 3:
        return None

    mean_x = statistics.fmean(xs)
    mean_y = statistics.fmean(ys)

    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]

    var_x = sum(x * x for x in dx)
    var_y = sum(y * y for y in dy)

    if var_x == 0 or var_y == 0:
        return None

    covariance = sum(x * y for x, y in zip(dx, dy))

    return covariance / math.sqrt(var_x * var_y)


def score_period(
    series: list[tuple[str, float]],
    period: int,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> tuple[Optional[float], int]:
    """
    Score a fixed Aroon period against forward return.

    Positive correlation = continuation-like relationship.
    Negative correlation = reversion-like relationship.

    Absolute magnitude is used for parameter ranking; sign is retained
    separately and must prove stable before a candidate is review-worthy.
    """

    values = [value for _, value in series]

    oscillator = aroon_oscillator(values, period)
    future = future_log_returns(values, FORWARD_HORIZON)

    xs: list[float] = []
    ys: list[float] = []

    for i, ((row_date, _), signal, future_return) in enumerate(
        zip(series, oscillator, future)
    ):
        if signal is None or future_return is None:
            continue

        if start_date is not None and row_date < start_date:
            continue

        if end_date is not None and row_date > end_date:
            continue

        future_index = i + FORWARD_HORIZON

        if future_index >= len(series):
            continue

        future_date = series[future_index][0]

        if end_date is not None and future_date > end_date:
            continue

        xs.append(signal)
        ys.append(future_return)

    if len(xs) < MIN_SCORE_SAMPLES:
        return None, len(xs)

    return pearson(xs, ys), len(xs)


def score_grid(
    series: list[tuple[str, float]],
    start_date: Optional[str],
    end_date: Optional[str],
) -> dict[int, float]:
    scores: dict[int, float] = {}

    for period in range(AROON_MIN, AROON_MAX + 1):
        correlation, _ = score_period(
            series,
            period,
            start_date=start_date,
            end_date=end_date,
        )

        if correlation is not None:
            scores[period] = correlation

    return scores


def choose_plateau(scores: dict[int, float]) -> tuple[int, tuple[int, int]]:
    if not scores:
        raise RuntimeError("No valid Aroon parameter scores.")

    best_period = max(scores, key=lambda p: abs(scores[p]))
    best_score = abs(scores[best_period])

    threshold = best_score * PLATEAU_FRACTION

    acceptable = {
        period
        for period, score in scores.items()
        if abs(score) >= threshold
    }

    left = best_period
    right = best_period

    while left - 1 in acceptable:
        left -= 1

    while right + 1 in acceptable:
        right += 1

    selected = round((left + right) / 2)

    return selected, (left, right)


def complete_validation_years(
    series: list[tuple[str, float]],
) -> list[int]:
    current_year = date.today().year

    years = sorted(
        {
            int(row_date[:4])
            for row_date, _ in series
            if int(row_date[:4]) < current_year
        }
    )

    return years[-VALIDATION_YEARS:]


def walk_forward(series: list[tuple[str, float]]) -> list[FoldResult]:
    results: list[FoldResult] = []

    for year in complete_validation_years(series):
        train_end = f"{year - 1}-12-31"
        validation_start = f"{year}-01-01"
        validation_end = f"{year}-12-31"

        training_scores = score_grid(
            series,
            start_date=None,
            end_date=train_end,
        )

        if not training_scores:
            continue

        selected, _ = choose_plateau(training_scores)

        train_corr = training_scores[selected]

        validation_corr, validation_samples = score_period(
            series,
            selected,
            start_date=validation_start,
            end_date=validation_end,
        )

        results.append(
            FoldResult(
                validation_year=year,
                selected_period=selected,
                training_correlation=train_corr,
                validation_correlation=validation_corr,
                validation_samples=validation_samples,
            )
        )

    return results


def mean_abs(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.fmean(abs(v) for v in values)


def sign_consistency(values: list[float]) -> Optional[float]:
    nonzero = [v for v in values if v != 0]

    if not nonzero:
        return None

    positive = sum(v > 0 for v in nonzero)
    negative = sum(v < 0 for v in nonzero)

    return max(positive, negative) / len(nonzero)


def dominant_sign(values: list[float]) -> str:
    nonzero = [v for v in values if v != 0]

    if not nonzero:
        return "undetermined"

    positive = sum(v > 0 for v in nonzero)
    negative = sum(v < 0 for v in nonzero)

    if positive > negative:
        return "continuation"

    if negative > positive:
        return "reversion"

    return "mixed"


def evaluate_fixed_period(
    series: list[tuple[str, float]],
    period: int,
    years: list[int],
) -> list[float]:
    values = []

    for year in years:
        correlation, _ = score_period(
            series,
            period,
            start_date=f"{year}-01-01",
            end_date=f"{year}-12-31",
        )

        if correlation is not None:
            values.append(correlation)

    return values


def calibration_decision(
    *,
    current_inside_plateau: bool,
    candidate_period: int,
    candidate_score: Optional[float],
    current_score: Optional[float],
    absolute_improvement: Optional[float],
    relative_improvement: Optional[float],
    candidate_consistency: Optional[float],
    validation_fold_count: int,
    walk_forward_period_span: Optional[int],
) -> tuple[str, str]:
    """
    This is deliberately conservative.

    The backoffice never promotes a parameter automatically.
    It either keeps the existing setting, asks for a wider search,
    or marks a candidate for human review.
    """

    if current_inside_plateau:
        return "KEEP_CURRENT", "Current period is already inside the final stable region."

    if candidate_period in (AROON_MIN, AROON_MAX):
        return (
            "EXPAND_SEARCH",
            "Best candidate sits on the search boundary; the tested range may be too narrow.",
        )

    if validation_fold_count < MIN_VALIDATION_FOLDS:
        return (
            "KEEP_CURRENT",
            "Too few usable validation folds to justify a parameter change.",
        )

    if candidate_score is None or current_score is None:
        return (
            "KEEP_CURRENT",
            "Candidate/current validation score comparison is incomplete.",
        )

    if absolute_improvement is None or absolute_improvement < MIN_ABS_CORR_IMPROVEMENT:
        return (
            "KEEP_CURRENT",
            "Absolute validation improvement is below the review threshold.",
        )

    if relative_improvement is None or relative_improvement < MIN_RELATIVE_IMPROVEMENT:
        return (
            "KEEP_CURRENT",
            "Relative validation improvement is below the review threshold.",
        )

    if candidate_consistency is None or candidate_consistency < MIN_SIGN_CONSISTENCY:
        return (
            "KEEP_CURRENT",
            "Forward relationship changes sign too often across validation years.",
        )

    if (
        walk_forward_period_span is None
        or walk_forward_period_span > MAX_WALK_FORWARD_PERIOD_SPAN
    ):
        return (
            "KEEP_CURRENT",
            "Walk-forward selected periods move too widely to call the parameter base stable.",
        )

    return (
        "REVIEW_CANDIDATE",
        "Candidate cleared improvement, sign-consistency, and parameter-stability gates.",
    )


def calibrate_pair(
    pair: str,
    series: list[tuple[str, float]],
    current_period: int,
) -> dict:
    current_year = date.today().year
    calibration_end = f"{current_year - 1}-12-31"

    final_scores = score_grid(
        series,
        start_date=None,
        end_date=calibration_end,
    )

    candidate_period, stable_range = choose_plateau(final_scores)

    folds = walk_forward(series)

    validation_years = [
        fold.validation_year
        for fold in folds
        if fold.validation_correlation is not None
    ]

    candidate_validation = evaluate_fixed_period(
        series,
        candidate_period,
        validation_years,
    )

    current_validation = evaluate_fixed_period(
        series,
        current_period,
        validation_years,
    )

    candidate_score = mean_abs(candidate_validation)
    current_score = mean_abs(current_validation)

    candidate_consistency = sign_consistency(candidate_validation)
    relation = dominant_sign(candidate_validation)

    absolute_improvement = None
    relative_improvement = None

    if candidate_score is not None and current_score is not None:
        absolute_improvement = candidate_score - current_score

        if current_score > 0:
            relative_improvement = absolute_improvement / current_score

    current_inside_plateau = (
        stable_range[0] <= current_period <= stable_range[1]
    )

    selected_periods = [
        fold.selected_period
        for fold in folds
        if fold.validation_correlation is not None
    ]

    walk_forward_period_span = (
        max(selected_periods) - min(selected_periods)
        if selected_periods
        else None
    )

    decision, decision_reason = calibration_decision(
        current_inside_plateau=current_inside_plateau,
        candidate_period=candidate_period,
        candidate_score=candidate_score,
        current_score=current_score,
        absolute_improvement=absolute_improvement,
        relative_improvement=relative_improvement,
        candidate_consistency=candidate_consistency,
        validation_fold_count=len(validation_years),
        walk_forward_period_span=walk_forward_period_span,
    )

    fold_json = []

    for fold in folds:
        fold_json.append(
            {
                "validation_year": fold.validation_year,
                "selected_period": fold.selected_period,
                "training_correlation": round(
                    fold.training_correlation, 8
                ),
                "validation_correlation": (
                    round(fold.validation_correlation, 8)
                    if fold.validation_correlation is not None
                    else None
                ),
                "validation_samples": fold.validation_samples,
            }
        )

    return {
        "candidate_period": candidate_period,
        "stable_range": [stable_range[0], stable_range[1]],
        "current_period": current_period,
        "current_inside_stable_range": current_inside_plateau,
        "candidate_validation_score": candidate_score,
        "current_validation_score": current_score,
        "absolute_improvement": absolute_improvement,
        "relative_improvement": relative_improvement,
        "sign_consistency": candidate_consistency,
        "observed_relation": relation,
        "walk_forward_period_span": walk_forward_period_span,
        "validation_fold_count": len(validation_years),
        "decision": decision,
        "decision_reason": decision_reason,
        "folds": fold_json,
    }


def load_profiles(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing profiles file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_profiles(path: Path, profiles: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(
            profiles,
            handle,
            indent=2,
            sort_keys=False,
        )
        handle.write("\n")


def update_profiles(
    profiles: dict,
    pair_results: dict[str, dict],
    source_start: str,
    first_observation: str,
    last_observation: str,
) -> dict:
    today = date.today().isoformat()

    profiles["generated_at"] = today
    profiles["status"] = "calibration_review"

    profiles["calibration_method"] = {
        "version": "aroon_walk_forward_v2",
        "source_start_requested": source_start,
        "first_observation": first_observation,
        "last_observation": last_observation,
        "forward_horizon_observations": FORWARD_HORIZON,
        "validation_years": VALIDATION_YEARS,
        "aroon_period_range": [AROON_MIN, AROON_MAX],
        "plateau_fraction": PLATEAU_FRACTION,
        "score": (
            "absolute Pearson correlation between Aroon oscillator "
            "and forward log return; correlation sign retained"
        ),
        "promotion": "none; candidates are flagged for review only",
        "review_thresholds": {
            "minimum_absolute_correlation_improvement":
                MIN_ABS_CORR_IMPROVEMENT,
            "minimum_relative_improvement":
                MIN_RELATIVE_IMPROVEMENT,
            "minimum_sign_consistency":
                MIN_SIGN_CONSISTENCY,
            "maximum_walk_forward_period_span":
                MAX_WALK_FORWARD_PERIOD_SPAN,
            "minimum_validation_folds":
                MIN_VALIDATION_FOLDS,
        },
    }

    for pair, result in pair_results.items():
        profile = profiles["pairs"][pair]

        profile["stable_ranges"]["aroon"] = result["stable_range"]

        profile["calibration"]["training_window"] = (
            f"{first_observation}/{date.today().year - 1}-12-31"
        )

        profile["calibration"]["validation_windows"] = [
            str(fold["validation_year"])
            for fold in result["folds"]
        ]

        profile["calibration"]["score"] = (
            result["candidate_validation_score"]
        )

        profile["calibration"]["neighbor_stability"] = {
            "stable_range": result["stable_range"],
            "current_inside_stable_range":
                result["current_inside_stable_range"],
            "walk_forward_period_span":
                result["walk_forward_period_span"],
        }

        profile["calibration"]["walk_forward_consistency"] = (
            result["sign_consistency"]
        )

        profile["calibration"]["decision"] = result["decision"]
        profile["calibration"]["decision_reason"] = result["decision_reason"]

        candidate = dict(profile["parameters"])
        candidate["aroon"] = result["candidate_period"]

        profile["calibration"]["candidate_parameters"] = candidate

        profile["calibration"]["minimum_improvement_required"] = {
            "absolute": MIN_ABS_CORR_IMPROVEMENT,
            "relative": MIN_RELATIVE_IMPROVEMENT,
        }

        profile["calibration"]["aroon_research"] = {
            "current_period": result["current_period"],
            "candidate_period": result["candidate_period"],
            "stable_range": result["stable_range"],
            "current_validation_score":
                result["current_validation_score"],
            "candidate_validation_score":
                result["candidate_validation_score"],
            "absolute_improvement":
                result["absolute_improvement"],
            "relative_improvement":
                result["relative_improvement"],
            "sign_consistency":
                result["sign_consistency"],
            "observed_relation":
                result["observed_relation"],
            "walk_forward_period_span":
                result["walk_forward_period_span"],
            "validation_fold_count":
                result["validation_fold_count"],
            "decision":
                result["decision"],
            "decision_reason":
                result["decision_reason"],
            "folds":
                result["folds"],
        }

    return profiles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Calibrate Aroon periods for FX Rotation Lab without "
            "automatically promoting parameter changes."
        )
    )

    parser.add_argument(
        "--profiles",
        default="profiles.json",
        help="Path to profiles.json",
    )

    parser.add_argument(
        "--start",
        default=DEFAULT_START,
        help="Earliest requested ECB observation date",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    profiles_path = Path(args.profiles)

    profiles = load_profiles(profiles_path)

    rows = fetch_ecb_rows(args.start)

    first_observation = rows[0]["date"]
    last_observation = rows[-1]["date"]

    print(
        f"Loaded {len(rows)} synchronized observations "
        f"from {first_observation} through {last_observation}"
    )

    results: dict[str, dict] = {}

    for pair in PAIRS:
        series = pair_series(rows, pair)

        current_period = int(
            profiles["pairs"][pair]["parameters"]["aroon"]
        )

        result = calibrate_pair(
            pair,
            series,
            current_period=current_period,
        )

        results[pair] = result

        stable = result["stable_range"]
        span = result["walk_forward_period_span"]

        print(
            f"{pair:7s} "
            f"current={current_period:2d} "
            f"candidate={result['candidate_period']:2d} "
            f"stable={stable[0]:2d}-{stable[1]:2d} "
            f"sign={result['sign_consistency']!s:>4} "
            f"wf_span={span!s:>2} "
            f"decision={result['decision']}"
        )

        print(f"         reason: {result['decision_reason']}")

    profiles = update_profiles(
        profiles,
        results,
        source_start=args.start,
        first_observation=first_observation,
        last_observation=last_observation,
    )

    write_profiles(profiles_path, profiles)

    print(f"Updated {profiles_path}")
    print("Active parameters were not automatically changed.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
