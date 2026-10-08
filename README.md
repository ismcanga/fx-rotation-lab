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
