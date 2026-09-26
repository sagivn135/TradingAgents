from __future__ import annotations

from dataclasses import replace

import pandas as pd
import pytest

from tradingagents.discovery import (
    ScreenConfig,
    discover_stocks,
    load_universe,
    write_discovery_report,
)


def _history(*, daily_return: float = 0.001, volume: float = 2_000_000) -> pd.DataFrame:
    dates = pd.bdate_range("2025-01-01", periods=300)
    close = pd.Series(
        [100 * (1 + daily_return) ** day for day in range(len(dates))], index=dates
    )
    return pd.DataFrame({"Date": dates, "Close": close.values, "Volume": volume})


@pytest.mark.unit
def test_discovery_ranks_momentum_and_records_point_in_time_metadata():
    frames = {
        "FAST": _history(daily_return=0.002),
        "SLOW": _history(daily_return=0.0005),
    }
    as_of = frames["FAST"]["Date"].iloc[-1].date().isoformat()

    result = discover_stocks(
        frames,
        as_of,
        history_loader=lambda ticker, _as_of: frames[ticker],
        universe_source="test universe",
        universe_observed_at="2025-01-01",
    )

    assert [item.ticker for item in result.candidates] == ["FAST", "SLOW"]
    assert result.candidates[0].score > result.candidates[1].score
    assert result.data_source == "Yahoo Finance via yfinance"
    assert result.candidates[0].observation_date == as_of
    assert result.retrieved_at_utc.endswith("+00:00")


@pytest.mark.unit
def test_discovery_never_uses_rows_after_as_of():
    base = _history(daily_return=0.001)
    as_of = base["Date"].iloc[-10].date().isoformat()
    future_date = base["Date"].iloc[-1] + pd.Timedelta(days=1)
    poisoned = pd.concat([
        base,
        pd.DataFrame({"Date": [future_date], "Close": [1_000_000], "Volume": [9_000_000]}),
    ], ignore_index=True)
    settings = replace(ScreenConfig(), max_staleness_days=20)

    clean = discover_stocks(
        ["TEST"], as_of, config=settings, history_loader=lambda *_: base
    ).candidates[0]
    with_future = discover_stocks(
        ["TEST"], as_of, config=settings, history_loader=lambda *_: poisoned
    ).candidates[0]

    assert with_future.close == clean.close
    assert with_future.momentum_6m == clean.momentum_6m
    assert with_future.observation_date == clean.observation_date


@pytest.mark.unit
def test_discovery_rejects_illiquid_and_broken_symbols_without_stopping_run():
    liquid = _history()
    illiquid = _history(volume=10)
    as_of = liquid["Date"].iloc[-1].date().isoformat()

    def loader(ticker, _as_of):
        if ticker == "BROKEN":
            raise RuntimeError("vendor unavailable")
        return liquid if ticker == "LIQUID" else illiquid

    result = discover_stocks(
        ["LIQUID", "ILLIQUID", "BROKEN"], as_of, history_loader=loader
    )

    assert [item.ticker for item in result.candidates] == ["LIQUID"]
    rejected = {item.ticker: item.reason for item in result.rejected}
    assert "average dollar volume" in rejected["ILLIQUID"]
    assert rejected["BROKEN"] == "vendor unavailable"


@pytest.mark.unit
def test_universe_metadata_and_reports_are_auditable(tmp_path):
    universe = tmp_path / "universe.txt"
    universe.write_text(
        "# source: test provider\n# observed_at: 2026-09-01\nnvda\nNVDA\naapl\n",
        encoding="utf-8",
    )
    tickers, metadata = load_universe(universe)
    assert tickers == ["NVDA", "AAPL"]
    assert metadata == {"source": "test provider", "observed_at": "2026-09-01"}

    frame = _history()
    as_of = frame["Date"].iloc[-1].date().isoformat()
    result = discover_stocks(
        ["NVDA"],
        as_of,
        history_loader=lambda *_: frame,
        universe_source=metadata["source"],
        universe_observed_at=metadata["observed_at"],
    )
    summary = write_discovery_report(result, tmp_path / "report")

    text = summary.read_text(encoding="utf-8")
    assert "Yahoo Finance via yfinance" in text
    assert "2026-09-01" in text
    assert (summary.parent / "discovery.json").exists()
    assert (summary.parent / "candidates.csv").exists()
