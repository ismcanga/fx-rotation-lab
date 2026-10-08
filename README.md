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
