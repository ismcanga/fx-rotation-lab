#!/usr/bin/env python3

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import statistics
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    from . import aroon_events as events
except ImportError:
    import aroon_events as events

PAIRS = events.PAIRS

DEFAULT_START = "2000-01-01"

PLATEAU_FRACTION = 0.975


@dataclass(frozen=True)
class GranularityPolicy:
    aroon_min: int
    aroon_max: int
    forward_horizon: int
    min_training_samples: int
    min_validation_samples: int
    validation_years_per_fold: int
    validation_fold_count: int
    min_validation_folds: int
    max_selected_period_span: int
    rationale: str


# Initial research policies, not optimized settings or significance guarantees.
# Periods and horizons count observations at the named signal granularity.
GRANULARITY_POLICIES = {
    "1D": GranularityPolicy(
        aroon_min=10, aroon_max=60, forward_horizon=5,
        min_training_samples=120, min_validation_samples=120,
        validation_years_per_fold=1, validation_fold_count=5,
        min_validation_folds=4, max_selected_period_span=15,
        rationale="Preserve v3 daily research and annual validation requirements.",
    ),
    "1W": GranularityPolicy(
        aroon_min=4, aroon_max=26, forward_horizon=5,
        min_training_samples=104, min_validation_samples=40,
        validation_years_per_fold=1, validation_fold_count=5,
        min_validation_folds=4, max_selected_period_span=6,
        rationale=(
            "About one to six months of lookback; two years of scored training "
            "observations and at least 40 within-year validation targets."
        ),
    ),
    "1M": GranularityPolicy(
        aroon_min=3, aroon_max=18, forward_horizon=5,
        min_training_samples=96, min_validation_samples=24,
        validation_years_per_fold=3, validation_fold_count=5,
        min_validation_folds=4, max_selected_period_span=4,
        rationale=(
            "Three to eighteen months of lookback; eight years of scored training "
            "observations. Three-year folds allow 24 validation targets."
        ),
    ),
    "1Q": GranularityPolicy(
        aroon_min=4, aroon_max=12, forward_horizon=4,
        min_training_samples=60, min_validation_samples=20,
        validation_years_per_fold=6, validation_fold_count=3,
        min_validation_folds=3, max_selected_period_span=3,
        rationale=(
            "One to three years of lookback; fifteen years of scored training "
            "observations and three six-year validation folds. Typical ECB "
            "history is expected to be insufficient."
        ),
    ),
}


@dataclass
class ScorePoint:
    period: int
    correlation: Optional[float]
    samples: int


@dataclass
class Plateau:
    start: int
    end: int
    representative: int
    sign: int
    best_abs_correlation: float
    touches_lower_boundary: bool
    touches_upper_boundary: bool


@dataclass
class FoldResult:
    training_end: str
    validation_start: str
    validation_end: str
    status: str
    selected_period: Optional[int]
    training_correlation: Optional[float]
    training_direction: Optional[int]
    validation_correlation: Optional[float]
    oriented_validation_correlation: Optional[float]
    validation_samples: Optional[int]
    training_plateaus: list[dict]


def sign_of(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def fetch_ecb_rows(start: str, as_of: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "from": start,
            "to": as_of,
            "quotes": "USD,CHF,JPY",
            "providers": "ecb",
        }
    )
    url = f"https://api.frankfurter.dev/v2/rates?{params}"

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "fx-rotation-lab-backoffice/4.0",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)

    by_date: dict[str, dict[str, float]] = {}

    for row in payload:
        row_date = row["date"]
        if not start <= row_date <= as_of:
            continue

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


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_source_snapshot(
    rows: list[dict],
    *,
    as_of: str,
    output_dir: Path,
) -> tuple[Path, str]:
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "kind": "fx_rotation_lab.ecb_snapshot.v1",
        "provider": "ECB via Frankfurter",
        "as_of": as_of,
        "observation_count": len(rows),
        "first_observation": rows[0]["date"],
        "last_observation": rows[-1]["date"],
        "rows": rows,
    }

    data = canonical_json_bytes(payload)
    digest = sha256_hex(data)

    path = output_dir / f"ecb_daily_{as_of}_{digest[:12]}.json"
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"Refusing to replace source snapshot: {path}")
    else:
        path.write_bytes(data)

    return path, digest


def period_end(observation_date: str, granularity: str) -> str:
    """Calendar completion date, separate from the last reference date.

    Weekly periods finalize on Sunday. No ECB holiday calendar or executable
    availability time is inferred from a date-only historical snapshot.
    """
    parsed = date.fromisoformat(observation_date)
    if granularity == "1D":
        return observation_date
    if granularity == "1W":
        return (parsed + timedelta(days=6 - parsed.weekday())).isoformat()
    if granularity not in ("1M", "1Q"):
        raise ValueError(f"Unsupported granularity: {granularity}")
    month = parsed.month
    if granularity == "1Q":
        month = ((month - 1) // 3 + 1) * 3
    return date(parsed.year, month, calendar.monthrange(parsed.year, month)[1]).isoformat()


def research_series(
    daily_series: list[tuple[str, float]],
    *,
    granularity: str,
    as_of: str,
) -> tuple[list[tuple[str, float]], dict]:
    # Shared candle construction owns OHLC, ISO-week and close conventions.
    bounded = [(d, value) for d, value in daily_series if d <= as_of]
    candles = events.to_candles(bounded, granularity)
    retained = [c for c in candles if period_end(c.end_date, granularity) <= as_of]
    reference_series = events.signal_series_from_candles(retained)
    # Scoring dates are when a bucket has finished, so a December reference in
    # an ISO week ending in January cannot leak into December training.
    series = [(period_end(d, granularity), value) for d, value in reference_series]
    return series, {
        "observations": len(series),
        "first_reference_date": reference_series[0][0] if series else None,
        "last_reference_date": reference_series[-1][0] if series else None,
        "first_available_date": series[0][0] if series else None,
        "last_available_date": series[-1][0] if series else None,
        "excluded_unfinished_candles": len(candles) - len(retained),
        "date_convention": "calendar_period_end; reference close retained separately",
        "reference_dates": [d for d, _ in reference_series],
        "availability_dates": [d for d, _ in series],
    }


def aroon_oscillator(values: list[float], period: int) -> list[Optional[float]]:
    """Reuse the event module's close-based Aroon, scaled to [-1, 1]."""
    return [
        point.separation / 100.0 if point.separation is not None else None
        for point in events.aroon(values, period)
    ]


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
    *,
    policy: GranularityPolicy,
    min_samples: Optional[int] = None,
) -> tuple[Optional[float], int]:
    values = [value for _, value in series]
    oscillator = aroon_oscillator(values, period)
    future = future_log_returns(values, policy.forward_horizon)

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

        future_index = i + policy.forward_horizon

        if future_index >= len(series):
            continue

        future_date = series[future_index][0]

        # No target may cross the explicit scoring boundary.
        if end_date is not None and future_date > end_date:
            continue

        xs.append(signal)
        ys.append(future_return)

    required = policy.min_training_samples if min_samples is None else min_samples
    if len(xs) < required:
        return None, len(xs)

    return pearson(xs, ys), len(xs)


def score_grid(
    series: list[tuple[str, float]],
    *,
    start_date: Optional[str],
    end_date: Optional[str],
    policy: GranularityPolicy,
) -> dict[int, ScorePoint]:
    result: dict[int, ScorePoint] = {}

    for period in range(policy.aroon_min, policy.aroon_max + 1):
        correlation, samples = score_period(
            series,
            period,
            start_date=start_date,
            end_date=end_date,
            policy=policy,
        )
        result[period] = ScorePoint(
            period=period,
            correlation=correlation,
            samples=samples,
        )

    return result


def all_near_best_plateaus(
    scores: dict[int, ScorePoint], *, policy: GranularityPolicy,
) -> list[Plateau]:
    """
    Find all contiguous near-best regions.

    Important policy:
    - ranking uses absolute correlation
    - regions NEVER join across correlation-sign changes
    - representative uses lower integer midpoint: (start + end) // 2
    """

    scores = {p: point for p, point in scores.items() if point.correlation is not None}
    if not scores:
        return []

    best_abs = max(abs(point.correlation) for point in scores.values())
    threshold = best_abs * PLATEAU_FRACTION

    eligible = {
        period: point
        for period, point in scores.items()
        if abs(point.correlation) >= threshold
    }

    plateaus: list[Plateau] = []
    visited: set[int] = set()

    for period in sorted(eligible):
        if period in visited:
            continue

        point = eligible[period]
        region_sign = sign_of(point.correlation)

        left = period
        right = period

        while (
            left - 1 in eligible
            and sign_of(eligible[left - 1].correlation) == region_sign
        ):
            left -= 1

        while (
            right + 1 in eligible
            and sign_of(eligible[right + 1].correlation) == region_sign
        ):
            right += 1

        for p in range(left, right + 1):
            visited.add(p)

        region_best = max(
            abs(scores[p].correlation)
            for p in range(left, right + 1)
        )

        plateaus.append(
            Plateau(
                start=left,
                end=right,
                representative=(left + right) // 2,
                sign=region_sign,
                best_abs_correlation=region_best,
                touches_lower_boundary=(left == policy.aroon_min),
                touches_upper_boundary=(right == policy.aroon_max),
            )
        )

    plateaus.sort(
        key=lambda p: (
            -p.best_abs_correlation,
            p.start,
            p.end,
        )
    )

    return plateaus


def select_primary_plateau(
    scores: dict[int, ScorePoint], *, policy: GranularityPolicy,
) -> tuple[int, Plateau, list[Plateau]]:
    if not scores:
        raise RuntimeError("No valid Aroon scores.")

    plateaus = all_near_best_plateaus(scores, policy=policy)

    if not plateaus:
        raise RuntimeError("No near-best Aroon plateaus.")

    primary = plateaus[0]

    return primary.representative, primary, plateaus


def validation_windows(*, as_of: str, policy: GranularityPolicy) -> list[tuple[str, str]]:
    """Nonoverlapping complete calendar blocks, anchored at the explicit cutoff."""
    last_year = date.fromisoformat(as_of).year
    width = policy.validation_years_per_fold
    first_year = last_year - width * policy.validation_fold_count + 1
    return [
        (f"{year}-01-01", f"{year + width - 1}-12-31")
        for year in range(first_year, last_year + 1, width)
    ]


def walk_forward(
    series: list[tuple[str, float]],
    *,
    as_of: str,
    policy: GranularityPolicy,
) -> list[FoldResult]:
    results: list[FoldResult] = []
    for validation_start, validation_end in validation_windows(as_of=as_of, policy=policy):
        train_end = (date.fromisoformat(validation_start) - timedelta(days=1)).isoformat()
        training_scores = score_grid(series, start_date=None, end_date=train_end, policy=policy)
        plateaus = all_near_best_plateaus(training_scores, policy=policy)
        if not plateaus:
            results.append(FoldResult(
                training_end=train_end,
                validation_start=validation_start,
                validation_end=validation_end,
                status="insufficient_training_evidence",
                selected_period=None,
                training_correlation=None,
                training_direction=None,
                validation_correlation=None,
                oriented_validation_correlation=None,
                validation_samples=None,
                training_plateaus=[],
            ))
            continue

        selected_period = plateaus[0].representative
        training_correlation = training_scores[selected_period].correlation
        assert training_correlation is not None
        training_direction = sign_of(training_correlation)
        validation_correlation, validation_samples = score_period(
            series, selected_period,
            start_date=validation_start, end_date=validation_end,
            policy=policy, min_samples=policy.min_validation_samples,
        )
        oriented = None
        if validation_correlation is not None and training_direction != 0:
            oriented = training_direction * validation_correlation
        results.append(FoldResult(
            training_end=train_end,
            validation_start=validation_start,
            validation_end=validation_end,
            status="scored" if oriented is not None else "insufficient_validation_evidence",
            selected_period=selected_period,
            training_correlation=training_correlation,
            training_direction=training_direction,
            validation_correlation=validation_correlation,
            oriented_validation_correlation=oriented,
            validation_samples=validation_samples,
            training_plateaus=[asdict(p) for p in plateaus],
        ))
    return results


def mean_or_none(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return statistics.fmean(values)


def matched_retrospective_diagnostics(
    series: list[tuple[str, float]],
    *,
    candidate_period: Optional[int],
    current_period: Optional[int],
    folds: list[FoldResult],
    policy: GranularityPolicy,
) -> dict:
    """Full-sample selection may use these windows; this is never validation."""
    result = {
        "label": "retrospective_not_validation",
        "status": "unavailable",
        "reason": None,
        "matched_window_count": 0,
        "matched_windows": [],
        "candidate_mean_absolute_correlation": None,
        "current_mean_absolute_correlation": None,
    }
    if current_period is None:
        result["reason"] = "no_active_parameter_for_this_granularity"
        return result
    if candidate_period is None:
        result["reason"] = "no_candidate"
        return result

    matched = []
    for fold in folds:
        candidate, c_samples = score_period(
            series, candidate_period, start_date=fold.validation_start,
            end_date=fold.validation_end, policy=policy,
            min_samples=policy.min_validation_samples,
        )
        current, a_samples = score_period(
            series, current_period, start_date=fold.validation_start,
            end_date=fold.validation_end, policy=policy,
            min_samples=policy.min_validation_samples,
        )
        if candidate is None or current is None:
            continue
        matched.append({
            "start": fold.validation_start,
            "end": fold.validation_end,
            "candidate_correlation": candidate,
            "candidate_samples": c_samples,
            "current_correlation": current,
            "current_samples": a_samples,
        })
    result.update({
        "status": "diagnostic_only" if matched else "unavailable",
        "reason": None if matched else "insufficient_matched_evidence",
        "matched_window_count": len(matched),
        "matched_windows": matched,
        "candidate_mean_absolute_correlation": mean_or_none([
            abs(row["candidate_correlation"]) for row in matched
        ]),
        "current_mean_absolute_correlation": mean_or_none([
            abs(row["current_correlation"]) for row in matched
        ]),
    })
    return result


def walk_forward_summary(folds: list[FoldResult]) -> dict:
    usable = [
        fold
        for fold in folds
        if fold.oriented_validation_correlation is not None
    ]

    oriented = [
        fold.oriented_validation_correlation
        for fold in usable
        if fold.oriented_validation_correlation is not None
    ]

    selected_periods = [fold.selected_period for fold in usable]

    positive_oriented_fraction = None

    if oriented:
        positive_oriented_fraction = (
            sum(v > 0 for v in oriented) / len(oriented)
        )

    period_span = None

    if selected_periods:
        period_span = max(selected_periods) - min(selected_periods)

    return {
        "usable_fold_count": len(usable),
        "mean_oriented_validation_correlation": mean_or_none(oriented),
        "median_oriented_validation_correlation": (
            statistics.median(oriented) if oriented else None
        ),
        "positive_oriented_fraction": positive_oriented_fraction,
        "selected_period_span": period_span,
        "selected_periods": selected_periods,
    }


def research_flags(
    *,
    primary_plateau: Optional[Plateau],
    all_plateaus: list[Plateau],
    wf_summary: dict,
    policy: GranularityPolicy,
) -> dict:
    return {
        "insufficient_full_sample_evidence": primary_plateau is None,
        "search_boundary_contact": bool(primary_plateau and (
            primary_plateau.touches_lower_boundary or primary_plateau.touches_upper_boundary
        )),
        "multiple_near_best_regions": len(all_plateaus) > 1,
        "insufficient_walk_forward_evidence": (
            wf_summary["usable_fold_count"] < policy.min_validation_folds
        ),
        "walk_forward_direction_instability": (
            wf_summary["positive_oriented_fraction"] is None
            or wf_summary["positive_oriented_fraction"] < 0.80
        ),
        "walk_forward_parameter_instability": (
            wf_summary["selected_period_span"] is None
            or wf_summary["selected_period_span"] > policy.max_selected_period_span
        ),
    }


def calibrate_pair(
    pair: str,
    series: list[tuple[str, float]],
    *,
    granularity: str,
    current_period: Optional[int],
    as_of: str,
    policy: GranularityPolicy,
) -> dict:
    final_scores = score_grid(series, start_date=None, end_date=as_of, policy=policy)
    all_plateaus = all_near_best_plateaus(final_scores, policy=policy)
    primary_plateau = all_plateaus[0] if all_plateaus else None
    folds = walk_forward(series, as_of=as_of, policy=policy)
    wf_summary = walk_forward_summary(folds)
    flags = research_flags(
        primary_plateau=primary_plateau, all_plateaus=all_plateaus,
        wf_summary=wf_summary, policy=policy,
    )
    sufficient = (
        primary_plateau is not None
        and not flags["insufficient_walk_forward_evidence"]
    )

    candidate_period = (
        primary_plateau.representative
        if sufficient
        else None
    )

    if not sufficient:
        candidate_quality = None
    elif any(
        flags[name]
        for name in (
            "search_boundary_contact",
            "multiple_near_best_regions",
            "walk_forward_direction_instability",
            "walk_forward_parameter_instability",
        )
    ):
        candidate_quality = "flagged"
    else:
        candidate_quality = "clean"
    return {
        "pair": pair,
        "granularity": granularity,
        "policy": asdict(policy),
        "status": "research_candidate" if sufficient else "insufficient_evidence",
        "candidate_period": candidate_period,
        "candidate_quality": candidate_quality,
        "current_period": current_period,
        "full_sample_diagnostic_period": (
            primary_plateau.representative if primary_plateau else None
        ),
        "primary_plateau": asdict(primary_plateau) if primary_plateau else None,
        "all_near_best_plateaus": [asdict(p) for p in all_plateaus],
        "score_grid": {
            str(period): asdict(point) for period, point in sorted(final_scores.items())
        },
        "walk_forward": {
            "summary": wf_summary,
            "folds": [asdict(fold) for fold in folds],
        },
        "retrospective_candidate_diagnostics": matched_retrospective_diagnostics(
            series, candidate_period=candidate_period, current_period=current_period,
            folds=folds, policy=policy,
        ),
        "flags": flags,
        "promotion": "none",
    }


def load_profiles(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing profiles file: {path}")

    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def validate_as_of(as_of: str) -> None:
    try:
        parsed = date.fromisoformat(as_of)
    except ValueError as exc:
        raise ValueError("--as-of must be YYYY-MM-DD") from exc
    if parsed.isoformat() != as_of or (parsed.month, parsed.day) != (12, 31):
        raise ValueError("--as-of must be a calendar-year boundary (YYYY-12-31).")
    if parsed >= datetime.now(timezone.utc).date():
        raise ValueError("--as-of must be a completed historical year.")


def validate_source_rows(rows: list[dict]) -> None:
    previous = None
    for row in rows:
        observation_date = row["date"]
        if date.fromisoformat(observation_date).isoformat() != observation_date:
            raise ValueError("Source dates must use canonical YYYY-MM-DD format.")
        if previous is not None and observation_date <= previous:
            raise ValueError("Source dates must be sorted and unique.")
        if any(not math.isfinite(row[c]) or row[c] <= 0 for c in ("EUR", "USD", "CHF", "JPY")):
            raise ValueError("Source rates must be positive and finite.")
        previous = observation_date


def build_research(
    *,
    pair_results: dict[str, dict],
    source_start: str,
    rows: list[dict],
    as_of: str,
    source_snapshot: Path,
    source_sha256: str,
    profiles_path: Path,
    profiles_sha256: str,
    granularities: list[str],
) -> dict:
    return {"research": {
        "kind": "fx_rotation_lab.aroon_research.v4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of": as_of,
        "granularity_policies": {g: asdict(GRANULARITY_POLICIES[g]) for g in granularities},
        "method": {
            "objective": "close_aroon_oscillator_vs_forward_endpoint_log_return_pearson",
            "objective_scope": (
                "continuous oscillator research, not transition or target-hit optimization"
            ),
            "entry_convention": "same-reference-price-anchor_descriptive_only",
            "candle_convention": (
                "aroon_events.to_candles; OHLC of sampled daily ECB references, not market "
                "OHLC"
            ),
            "signal_values": "aroon_events.signal_series_from_candles; closes only",
            "availability_convention": (
                "calendar bucket end; unfinished buckets excluded at as-of and fold "
                "boundaries"
            ),
            "source_completeness": (
                "calendar completion assumes supplied daily observations are complete; "
                "missing releases are not inferred"
            ),
            "horizon_unit": (
                "observations at each independently named granularity, not calendar "
                "durations"
            ),
            "excursion_scoring": (
                "not part of this objective; event evaluation retains intervening daily "
                "observations"
            ),
            "plateau_fraction": PLATEAU_FRACTION,
            "plateau_policy": (
                "absolute-correlation threshold; contiguous same-sign regions; lower "
                "integer midpoint"
            ),
            "walk_forward_validation": (
                "expanding training; period and direction selected from training only; "
                "sign(training correlation) * validation correlation"
            ),
            "scoring_boundary": (
                "signal and forward endpoint availability must lie inside the scoring "
                "window; prior observations allowed for indicator warmup"
            ),
            "candidate_policy": (
                "null unless full-sample scores and policy minimum usable folds exist; a "
                "candidate is not an approval or proof of edge"
            ),
            "candidate_diagnostics": (
                "retrospective_not_validation; walk-forward validates the selection "
                "process, not the final full-sample period"
            ),
            "uncertainty": (
                "overlapping targets are dependent; sample gates are not effective sample "
                "size or significance tests"
            ),
            "promotion": "none",
        },
        "source": {
            "provider": "ECB via Frankfurter",
            "requested_start": source_start,
            "first_observation": rows[0]["date"],
            "last_observation": rows[-1]["date"],
            "observation_count": len(rows),
            "snapshot_path": str(source_snapshot),
            "snapshot_sha256": source_sha256,
            "selection": "requested_start <= observation date <= as_of; source file untouched",
        },
        "active_profile_reference": {
            "path": str(profiles_path),
            "sha256": profiles_sha256,
            "scope": "legacy daily active Aroon used only as 1D retrospective comparator",
            "access": "read_only",
        },
        "implementation_sha256": {
            "calibrate.py": sha256_hex(Path(__file__).read_bytes()),
            "aroon_events.py": sha256_hex(Path(events.__file__).read_bytes()),
        },
        "pairs": pair_results,
        "promotion": "none",
    }}


def build_web_research(*, research_payload: dict) -> dict:
    research = research_payload["research"]
    pairs = {}

    for pair, pair_result in research["pairs"].items():
        granularities = {}

        for granularity, result in pair_result["granularities"].items():
            wf = result["walk_forward"]["summary"]

            granularities[granularity] = {
                "status": result["status"],
                "candidate_period": result["candidate_period"],
                "candidate_quality": result.get("candidate_quality"),
                "flags": [
                    name
                    for name, enabled in result["flags"].items()
                    if enabled
                ],
                "usable_fold_count": wf["usable_fold_count"],
                "mean_oriented_validation_correlation": (
                    wf["mean_oriented_validation_correlation"]
                ),
                "positive_oriented_fraction": (
                    wf["positive_oriented_fraction"]
                ),
            }

        pairs[pair] = {"granularities": granularities}

    return {
        "kind": "fx_rotation_lab.aroon_web.v1",
        "as_of": research["as_of"],
        "source_sha256": research["source"]["snapshot_sha256"],
        "pairs": pairs,
        "promotion": "none",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Granularity-aware Aroon research; no active parameter writes or promotion.",
    )
    parser.add_argument(
        "--profiles", default="profiles.json", help="Read-only active daily profile reference",
    )
    parser.add_argument(
        "--start", default=DEFAULT_START,
        help="Earliest included source observation, YYYY-MM-DD",
    )
    parser.add_argument(
        "--as-of", required=True,
        help="Explicit completed historical year cutoff, YYYY-12-31",
    )
    parser.add_argument(
        "--snapshot", help="Existing ECB snapshot for offline research; never rewritten",
    )
    parser.add_argument(
        "--source-dir", default="backoffice/artifacts",
        help="Archive directory when fetching new source data",
    )
    parser.add_argument(
        "--output",
        help="Separate research JSON; default backoffice/artifacts/aroon_research_<as-of>.json",
    )
    parser.add_argument(
        "--web-output",
        help=(
            "Compact web JSON; default "
            "backoffice/artifacts/aroon_web_<as-of>.json"
        ),
    )
    parser.add_argument("--pairs", nargs="+", choices=PAIRS, default=PAIRS)
    parser.add_argument(
        "--granularities", nargs="+", choices=events.GRANULARITIES,
        default=list(events.GRANULARITIES),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    validate_as_of(args.as_of)
    if date.fromisoformat(args.start).isoformat() != args.start or args.start > args.as_of:
        raise ValueError("--start must be a canonical date at or before --as-of.")
    profiles_path = Path(args.profiles)
    profiles = load_profiles(profiles_path)
    profiles_hash = sha256_hex(profiles_path.read_bytes())
    output = Path(args.output or f"backoffice/artifacts/aroon_research_{args.as_of}.json")
    web_output = Path(
        args.web_output or f"backoffice/artifacts/aroon_web_{args.as_of}.json"
    )
    protected = [profiles_path, Path(__file__).resolve().parents[1] / "profiles.json"]
    if args.snapshot:
        protected.append(Path(args.snapshot))
    if output.resolve() in {p.resolve() for p in protected}:
        raise ValueError("Research output must not replace active profiles or the source snapshot.")

    if web_output.resolve() in {p.resolve() for p in protected}:
        raise ValueError(
            "Web output must not replace active profiles or the source snapshot."
        )

    if web_output.resolve() == output.resolve():
        raise ValueError(
            "Research output and web output must be separate files."
        )
    if args.snapshot:
        source_path = Path(args.snapshot)
        rows = events.load_snapshot(source_path)
        source_hash = sha256_hex(source_path.read_bytes())
    else:
        rows = fetch_ecb_rows(args.start, args.as_of)
        validate_source_rows(rows)
        source_path, source_hash = archive_source_snapshot(
            rows, as_of=args.as_of, output_dir=Path(args.source_dir),
        )
    validate_source_rows(rows)
    rows = [r for r in rows if args.start <= r["date"] <= args.as_of]
    if not rows:
        raise ValueError("No synchronized observations inside the requested source interval.")
    if output.resolve() == source_path.resolve():
        raise ValueError("Research output must not replace the source snapshot.")
    if web_output.resolve() == source_path.resolve():
        raise ValueError("Web output must not replace the source snapshot.")
    print(f"Loaded {len(rows)} observations: {rows[0]['date']} through {rows[-1]['date']}")
    print(f"As-of: {args.as_of}; source: {source_path}; SHA256: {source_hash}")

    results = {}
    granularities = list(dict.fromkeys(args.granularities))
    for pair in dict.fromkeys(args.pairs):
        daily = events.pair_series(rows, pair)
        per_granularity = {}
        for granularity in granularities:
            policy = GRANULARITY_POLICIES[granularity]
            series, series_metadata = research_series(
                daily, granularity=granularity, as_of=args.as_of,
            )
            # The legacy profile is daily; never copy it to another granularity.
            current = (
                int(profiles["pairs"][pair]["parameters"]["aroon"])
                if granularity == "1D" else None
            )
            result = calibrate_pair(
                pair, series, granularity=granularity, current_period=current,
                as_of=args.as_of, policy=policy,
            )
            result["signal_series"] = series_metadata
            per_granularity[granularity] = result
            wf = result["walk_forward"]["summary"]
            print(
                f"{pair} {granularity}: {result['status']}; "
                f"candidate={result['candidate_period']}; "
                f"usable folds={wf['usable_fold_count']}; "
                f"required={policy.min_validation_folds}"
            )
        results[pair] = {"granularities": per_granularity}

    payload = build_research(
        pair_results=results, source_start=args.start, rows=rows,
        as_of=args.as_of, source_snapshot=source_path, source_sha256=source_hash,
        profiles_path=profiles_path, profiles_sha256=profiles_hash, granularities=granularities,
    )
    
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    web_payload = build_web_research(research_payload=payload)
    web_output.parent.mkdir(parents=True, exist_ok=True)
    web_output.write_text(
        json.dumps(web_payload, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    print(f"Wrote research artifact: {output}")
    print(f"Wrote web artifact: {web_output}")
    print("Active profiles and parameters were not changed. No candidate was promoted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
