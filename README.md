# FX Rotation Lab

A small public, static FX observation lab for USD, CHF, EUR and JPY.

The page is deliberately not a trading terminal. It focuses on one currency pair at a time and gives indicator panels enough space to be useful.

## Current features

- Six pairs: USD/CHF, USD/EUR, USD/JPY, EUR/CHF, EUR/JPY and CHF/JPY
- One selected pair at a time
- Aroon
- Close-based stochastic
- Kaufman efficiency ratio
- Small cross-currency context strip
- Pair-specific indicator parameters stored in the browser
- Exploration / locked parameter state
- Copyable deterministic analysis packet for ChatGPT
- Local move journal stored in browser localStorage
- Static GitHub Pages compatible; no backend or API key required

## Data

The page retrieves daily reference-rate history through the Frankfurter API pinned to the ECB provider.

These rates are used for observation and reproducible indicators. They are not executable broker bid/ask quotes and do not include spreads, commissions, settlement effects or interest.

Cross-rates are derived from synchronized EUR-based observations.

## Important stochastic limitation

The current data source provides daily reference observations rather than OHLC bars. Therefore the stochastic indicator uses the rolling range of daily reference values. It should not be confused with a textbook high/low/close stochastic calculated from market OHLC data.

## Pair profiles

Each pair has its own parameters in browser localStorage.

During exploration, parameters may be edited and saved. A profile can then be locked and timestamped for forward observation. Unlocking does not preserve an immutable audit history yet; that can be added later if useful.

## Deployment

For GitHub Pages, place `index.html` in the repository root and publish from the `main` branch `/ (root)`.


## Indicator conventions

Periods count synchronized observations, not elapsed calendar days.

- **Aroon** uses `N` observations, includes the current observation, divides by `N-1`, and uses the most recent tied high or low. A completely flat window is treated as `Aroon Up = 0` and `Aroon Down = 0` in this project because the window contains no directional extreme information.
- **Range position (stochastic)** uses the current daily reference observation relative to the minimum and maximum of the latest `N` daily reference observations. A flat range returns `%K = 50`. `%D` is a simple arithmetic average of the latest smoothing-period `%K` values.
- **Kaufman efficiency ratio** uses `N` changes across `N+1` observations. A completely flat path returns `0`.
- Indicator calculations use fetched warm-up observations before the visible history window. Changing the visible history must not change an indicator value for an overlapping date.
- Missing synchronized dates are not forward-filled.

## Peer context

For a selected pair `A/B`, the displayed context for A is the equal-weight arithmetic average of A's 5-observation percentage returns against the other two currencies in the four-currency universe, excluding B. B is treated symmetrically.

Example while viewing USD/JPY:

- USD context averages USD versus CHF and EUR.
- JPY context averages JPY versus CHF and EUR.

The label therefore says **vs other peers**, not breadth.

## Parameter profiles

Parameter inputs accept integers only:

- Aroon: 5–120 observations
- Range position: 5–120 observations
- Stochastic smoothing: 1–20 observations
- Efficiency ratio: 2–120 changes

Invalid or temporarily empty inputs do not recalculate the charts and cannot be saved or locked.

A locked profile freezes the parameter values only. New market observations continue to flow through those parameters. It does not archive or freeze the underlying dataset.

## Retrieval status

The page stores the timestamp of the last successful data fetch separately from the time an analysis packet is generated. If a later refresh fails, retained charts remain visible and the status says that older successfully retrieved data is still being shown.

## Granularity-aware calibration research

`backoffice/calibrate.py` writes a separate v4 research artifact. It reads
`profiles.json` only as a reference for the existing daily active period; it does
not rewrite the file, its schema, or active parameter metadata. Coarse research
has no active-period comparator. Candidates are never automatically promoted.

Run against an archived snapshot without network access:

```sh
python3 -B backoffice/calibrate.py \
  --as-of 2025-12-31 \
  --snapshot backoffice/artifacts/ecb_daily_2025-12-31.json \
  --output backoffice/artifacts/aroon_research_2025-12-31.json
```

`--as-of` is required and must name a completed historical year end. `--start`
defaults to `2000-01-01`; source rows outside that closed date interval are
excluded before aggregation. Omit `--snapshot` to fetch and archive ECB data.
New source filenames include a content-hash suffix; an existing source snapshot
is never replaced. Optional `--pairs USD/CHF` and `--granularities 1D 1W` bound
a research run. Each output is a standalone artifact, not a merge into earlier
research; use different output paths to retain multiple runs.

Each `(pair, granularity)` is researched separately. These initial policies are
declared in `GRANULARITY_POLICIES`, not fitted to the resulting scores:

| Granularity | Aroon range | Forward observations | Minimum training / validation samples | Validation folds | Minimum usable folds |
|---|---:|---:|---:|---|---:|
| 1D | 10–60 | 5 daily | 120 / 120 | 5 × 1 year | 4 |
| 1W | 4–26 | 5 weekly | 104 / 40 | 5 × 1 year | 4 |
| 1M | 3–18 | 5 monthly | 96 / 24 | 5 × 3 years | 4 |
| 1Q | 4–12 | 4 quarterly | 60 / 20 | 3 × 6 years | 3 |

Counts refer to eligible targets after indicator warmup and endpoint-boundary
exclusion. Monthly and quarterly annual folds would have too few observations;
longer, nonoverlapping calendar blocks are explicit policy. Training expands up
to the day before each block. Period and correlation direction are selected
using training data only, with contiguous same-sign near-best plateaus and the
lower integer midpoint. No target endpoint can cross a training or validation
boundary; pre-window data can supply indicator warmup.

Candles and Aroon reuse `aroon_events.py`. Candle OHLC is calculated from daily
reference observations, not market OHLC, and signals use candle closes only.
For calibration, each coarse close becomes available at calendar period end
(Sunday for ISO weeks). Unfinished buckets are excluded at the cutoff. Reference
dates and availability dates are retained separately, preventing a December
reference in a week ending in January from entering December training. Calendar
completion assumes the supplied daily source is complete; it does not prove
that no ECB releases are missing or establish an executable fill time.

The objective remains the v3 Pearson correlation of close-based Aroon oscillator
with a forward endpoint log return. This migration does **not** optimize
transition events, stochastic confirmation, or 0.5% target-hit rates. Event
excursions remain a separate experiment using intervening daily observations.

Results live at `research.pairs[PAIR].granularities[GRANULARITY]`, with policy,
score grid, fold boundaries, diagnostics, flags, and `promotion: "none"`.
Insufficient full-sample or walk-forward evidence yields `candidate_period: null`
and `status: "insufficient_evidence"`; unscored periods and folds remain visible.
Any full-sample plateau retained in this case is a retrospective diagnostic only.
An emitted candidate is a research proposal, not proof of advantage. Walk-forward
scores evaluate the selection procedure, not the final full-sample period.
Overlapping targets are dependent; the sample gates are not significance tests.

Artifacts retain the explicit cutoff, requested source start, first/last included
observations, original snapshot path and SHA256, read-only profile hash,
implementation hashes, and policies. The manual GitHub workflow requires a cutoff
and uploads research and source artifacts, without committing or promoting them.

Validation commands:

```sh
python3 -B backoffice/fixtures.py
python3 -B -m unittest discover -s backoffice -p 'test_*.py'
```
