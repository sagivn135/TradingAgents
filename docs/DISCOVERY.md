# Stock discovery

`discover` turns the single-ticker workflow into a two-stage research pipeline:

1. A deterministic point-in-time screen ranks a stock universe.
2. TradingAgents can analyze only the highest-ranked candidates.

The first stage is intentionally independent of an LLM. This makes the shortlist
repeatable, inexpensive, and suitable for later walk-forward evaluation. A high
screen score is a research priority, not a buy instruction. Brokerage execution
is outside the project scope.

## Running it

Activate the repository environment and run a quick screen:

```sh
.venv/bin/python -m cli.main discover
```

Run the agents on the three leading candidates:

```sh
.venv/bin/python -m cli.main discover --analyze-top 3 --checkpoint
```

Use an explicit universe or historical cutoff:

```sh
.venv/bin/python -m cli.main discover \
  --tickers AAPL,MSFT,NVDA,AMZN,META \
  --as-of 2026-09-25 \
  --analyze-top 2
```

Each run writes `summary.md`, `candidates.csv`, and `discovery.json` under the
configured results directory. Agent reports, when requested, are stored below
the same run directory.

## Data contract

- Price source: Yahoo Finance, accessed through `yfinance` and the existing
  cached OHLCV adapter.
- Price observation timestamp: the date of the latest eligible daily bar. It is
  stored on every candidate.
- Retrieval timestamp: UTC timestamp written on every discovery result.
- Point-in-time rule: all rows after `--as-of` are removed before metrics are
  computed. A stale last row is rejected.
- Universe source: stored separately from the price source. The bundled starter
  universe is project-maintained, reviewed on the date in its header, and is not
  represented as a live index membership list.
- Historical-universe limitation: the bundled list must not be treated as proof
  that all of its symbols belonged to a particular index on an earlier date.
  A survivorship-bias-safe backtest needs a dated constituent dataset whose
  observation date is no later than each simulated run date.
- Missing data: a symbol is rejected with its reason. Values are never invented.

## Ranking method

Eligibility requires at least 253 observations, a minimum price and 20-day
average dollar volume, positive six-month momentum, and a close above the
200-day moving average. Eligible stocks receive a cross-sectional percentile
score from 3/6/12-month momentum, distance above the 50-day average, proximity
to the 52-week high, and lower 63-day realized volatility.

This is the first measurable baseline. A Qlib model, alternative data, and
portfolio optimization should be added behind this same timestamped result
contract only after walk-forward results show that they improve this baseline.
