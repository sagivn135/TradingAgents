"""Point-in-time stock discovery and optional TradingAgents research.

The screen is deliberately deterministic: an LLM never chooses the universe or
computes the rank.  This keeps the first stage cheap, reproducible, and suitable
for backtesting.  The expensive multi-agent graph is reserved for the highest
ranked candidates.
"""

from __future__ import annotations

import csv
import json
import math
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from tradingagents.dataflows.symbols import normalize_symbol, safe_ticker_component
from tradingagents.dataflows.vendors.yahoo.ohlcv import load_ohlcv

DATA_SOURCE = "Yahoo Finance via yfinance"
DATA_SOURCE_URL = "https://finance.yahoo.com/"
DEFAULT_UNIVERSE_PATH = Path(__file__).with_name("universes") / "us_liquid_starter.txt"


@dataclass(frozen=True)
class ScreenConfig:
    """Hard eligibility rules and cross-sectional score weights."""

    min_price: float = 5.0
    min_avg_dollar_volume: float = 20_000_000.0
    min_history_rows: int = 253
    max_staleness_days: int = 10
    require_above_sma200: bool = True
    require_positive_6m_momentum: bool = True
    workers: int = 4


@dataclass
class Candidate:
    ticker: str
    observation_date: str
    close: float
    avg_dollar_volume_20d: float
    momentum_3m: float
    momentum_6m: float
    momentum_12m: float
    distance_above_sma50: float
    distance_above_sma200: float
    distance_from_52w_high: float
    annualized_volatility_63d: float
    score: float = 0.0
    rank: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Rejection:
    ticker: str
    reason: str


@dataclass
class AgentAnalysis:
    ticker: str
    signal: str
    report_path: str | None = None
    error: str | None = None


@dataclass
class DiscoveryResult:
    as_of_date: str
    retrieved_at_utc: str
    data_source: str
    data_source_url: str
    universe_source: str
    universe_observed_at: str
    methodology: str
    configuration: dict[str, Any]
    candidates: list[Candidate]
    rejected: list[Rejection]
    analyses: list[AgentAnalysis] = field(default_factory=list)


HistoryLoader = Callable[[str, str], pd.DataFrame]


def load_universe(path: str | Path = DEFAULT_UNIVERSE_PATH) -> tuple[list[str], dict[str, str]]:
    """Read a versioned universe file, ignoring comments and duplicate symbols."""
    universe_path = Path(path)
    metadata: dict[str, str] = {"source": str(universe_path), "observed_at": "unknown"}
    tickers: list[str] = []
    seen: set[str] = set()
    for raw_line in universe_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            if ":" in line:
                key, value = line[1:].split(":", 1)
                metadata[key.strip().lower()] = value.strip()
            continue
        ticker = normalize_symbol(line)
        safe_ticker_component(ticker)
        if ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    if not tickers:
        raise ValueError(f"Universe contains no tickers: {universe_path}")
    return tickers, metadata


def parse_tickers(value: str) -> list[str]:
    """Parse, normalize and de-duplicate a comma-separated ticker list."""
    tickers: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        if not raw.strip():
            continue
        ticker = normalize_symbol(raw)
        safe_ticker_component(ticker)
        if ticker not in seen:
            tickers.append(ticker)
            seen.add(ticker)
    if not tickers:
        raise ValueError("No ticker supplied")
    return tickers


def _canonical_date(value: str | date) -> str:
    text = value.isoformat() if isinstance(value, date) else str(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"as-of date must use YYYY-MM-DD, got {text!r}") from exc
    if parsed > datetime.now().date():
        raise ValueError(f"as-of date cannot be in the future: {text}")
    return parsed.isoformat()


def _default_history_loader(ticker: str, as_of: str) -> pd.DataFrame:
    return load_ohlcv(ticker, as_of, fill_gaps=False)


def _normalized_history(frame: pd.DataFrame, as_of: str) -> pd.DataFrame:
    data = frame.copy()
    if "Date" in data.columns:
        dates = pd.to_datetime(data.pop("Date"), errors="coerce", utc=True).dt.tz_localize(None)
        data.index = dates
    else:
        dates = pd.to_datetime(data.index, errors="coerce", utc=True)
        data.index = dates.tz_localize(None)
    data = data.loc[data.index.notna()]
    data = data.loc[data.index.normalize() <= pd.Timestamp(as_of)]
    data = data[~data.index.duplicated(keep="last")].sort_index()
    for column in ("Close", "Volume"):
        if column not in data.columns:
            raise ValueError(f"price history has no {column} column")
        data[column] = pd.to_numeric(data[column], errors="coerce")
    return data.dropna(subset=["Close", "Volume"])


def _measure(ticker: str, frame: pd.DataFrame, as_of: str, config: ScreenConfig) -> Candidate:
    data = _normalized_history(frame, as_of)
    if len(data) < config.min_history_rows:
        raise ValueError(f"only {len(data)} usable rows; need {config.min_history_rows}")

    latest = data.index[-1].normalize()
    staleness = (pd.Timestamp(as_of) - latest).days
    if staleness > config.max_staleness_days:
        raise ValueError(f"latest observation {latest.date()} is {staleness} days stale")

    close = data["Close"].astype(float)
    volume = data["Volume"].astype(float)
    last = float(close.iloc[-1])
    if not math.isfinite(last) or last < config.min_price:
        raise ValueError(f"close {last:.2f} is below minimum {config.min_price:.2f}")

    dollar_volume = float((close.tail(20) * volume.tail(20)).mean())
    if not math.isfinite(dollar_volume) or dollar_volume < config.min_avg_dollar_volume:
        raise ValueError(
            f"20-day average dollar volume {dollar_volume:,.0f} is below "
            f"{config.min_avg_dollar_volume:,.0f}"
        )

    sma50 = float(close.tail(50).mean())
    sma200 = float(close.tail(200).mean())
    momentum_3m = last / float(close.iloc[-64]) - 1
    momentum_6m = last / float(close.iloc[-127]) - 1
    momentum_12m = last / float(close.iloc[-253]) - 1
    above_50 = last / sma50 - 1
    above_200 = last / sma200 - 1
    from_high = last / float(close.tail(252).max()) - 1
    volatility = float(close.pct_change().tail(63).std() * math.sqrt(252))

    if config.require_above_sma200 and above_200 <= 0:
        raise ValueError("close is not above the 200-day moving average")
    if config.require_positive_6m_momentum and momentum_6m <= 0:
        raise ValueError("six-month momentum is not positive")

    reasons = [
        f"6m momentum {momentum_6m:+.1%}",
        f"{above_200:+.1%} vs 200d average",
        f"{from_high:.1%} from 52w high",
        f"20d dollar volume ${dollar_volume / 1_000_000:.0f}M",
    ]
    return Candidate(
        ticker=ticker,
        observation_date=latest.date().isoformat(),
        close=last,
        avg_dollar_volume_20d=dollar_volume,
        momentum_3m=momentum_3m,
        momentum_6m=momentum_6m,
        momentum_12m=momentum_12m,
        distance_above_sma50=above_50,
        distance_above_sma200=above_200,
        distance_from_52w_high=from_high,
        annualized_volatility_63d=volatility,
        reasons=reasons,
    )


def _rank(candidates: list[Candidate]) -> list[Candidate]:
    if not candidates:
        return []
    metrics = pd.DataFrame([asdict(candidate) for candidate in candidates])
    weights = {
        "momentum_3m": 0.20,
        "momentum_6m": 0.25,
        "momentum_12m": 0.20,
        "distance_above_sma50": 0.15,
        "distance_from_52w_high": 0.10,
        "annualized_volatility_63d": -0.10,
    }
    score = pd.Series(0.0, index=metrics.index)
    for column, weight in weights.items():
        percentile = metrics[column].rank(method="average", pct=True)
        score += percentile * abs(weight) if weight > 0 else (1 - percentile) * abs(weight)
    for index, candidate in enumerate(candidates):
        candidate.score = round(float(score.iloc[index] * 100), 2)
    ranked = sorted(candidates, key=lambda item: (-item.score, item.ticker))
    for position, candidate in enumerate(ranked, start=1):
        candidate.rank = position
    return ranked


def discover_stocks(
    tickers: Iterable[str],
    as_of: str | date,
    *,
    config: ScreenConfig | None = None,
    history_loader: HistoryLoader | None = None,
    universe_source: str = "caller-supplied universe",
    universe_observed_at: str = "unknown",
) -> DiscoveryResult:
    """Screen and rank stocks using only observations available by ``as_of``."""
    canonical_as_of = _canonical_date(as_of)
    settings = config or ScreenConfig()
    loader = history_loader or _default_history_loader
    names = list(dict.fromkeys(normalize_symbol(ticker) for ticker in tickers))
    if not names:
        raise ValueError("Discovery needs at least one ticker")

    candidates: list[Candidate] = []
    rejected: list[Rejection] = []

    def fetch(ticker: str) -> Candidate:
        safe_ticker_component(ticker)
        return _measure(ticker, loader(ticker, canonical_as_of), canonical_as_of, settings)

    if history_loader is not None or settings.workers <= 1:
        for ticker in names:
            try:
                candidates.append(fetch(ticker))
            except Exception as exc:
                rejected.append(Rejection(ticker, str(exc)))
    else:
        with ThreadPoolExecutor(max_workers=settings.workers) as executor:
            futures = {executor.submit(fetch, ticker): ticker for ticker in names}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    candidates.append(future.result())
                except Exception as exc:
                    rejected.append(Rejection(ticker, str(exc)))

    rejected.sort(key=lambda item: item.ticker)
    return DiscoveryResult(
        as_of_date=canonical_as_of,
        retrieved_at_utc=datetime.now(timezone.utc).isoformat(),
        data_source=DATA_SOURCE,
        data_source_url=DATA_SOURCE_URL,
        universe_source=universe_source,
        universe_observed_at=universe_observed_at,
        methodology="liquidity + 3/6/12m momentum + 50/200d trend + 52w high + volatility",
        configuration=asdict(settings),
        candidates=_rank(candidates),
        rejected=rejected,
    )


def analyze_top_candidates(
    result: DiscoveryResult,
    count: int,
    output_dir: str | Path,
    *,
    selected_analysts: Iterable[str] = ("market", "social", "news", "fundamentals"),
    graph_config: dict[str, Any] | None = None,
) -> list[AgentAnalysis]:
    """Run the multi-agent graph for the top candidates and save its reports."""
    if count <= 0:
        return []
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    graph = TradingAgentsGraph(
        selected_analysts=tuple(selected_analysts),
        config=graph_config or dict(DEFAULT_CONFIG),
    )
    analyses: list[AgentAnalysis] = []
    base = Path(output_dir) / "analysis"
    for candidate in result.candidates[:count]:
        try:
            state, signal = graph.propagate(candidate.ticker, result.as_of_date)
            report_path = graph.save_reports(state, candidate.ticker, base / candidate.ticker)
            analyses.append(AgentAnalysis(candidate.ticker, signal, str(report_path)))
        except Exception as exc:
            analyses.append(AgentAnalysis(candidate.ticker, "ERROR", error=str(exc)))
    result.analyses = analyses
    return analyses


def write_discovery_report(result: DiscoveryResult, output_dir: str | Path) -> Path:
    """Persist machine-readable data plus a compact, reviewable Markdown report."""
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    payload = asdict(result)
    (destination / "discovery.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    candidate_fields = list(Candidate.__dataclass_fields__)
    with (destination / "candidates.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=candidate_fields)
        writer.writeheader()
        for candidate in result.candidates:
            row = asdict(candidate)
            row["reasons"] = " | ".join(row["reasons"])
            writer.writerow(row)

    analyses = {item.ticker: item for item in result.analyses}
    lines = [
        "# Stock discovery report",
        "",
        f"- As-of date: `{result.as_of_date}`",
        f"- Retrieved at (UTC): `{result.retrieved_at_utc}`",
        f"- Price source: [{result.data_source}]({result.data_source_url})",
        f"- Universe source: `{result.universe_source}`",
        f"- Universe observed/reviewed at: `{result.universe_observed_at}`",
        f"- Method: {result.methodology}",
        "",
        "This is a research shortlist. It is not an instruction to trade and it does not place orders.",
        "",
        "| Rank | Ticker | Score | Close | 3m | 6m | 12m | vs SMA200 | Agent signal |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for candidate in result.candidates:
        analysis = analyses.get(candidate.ticker)
        signal = analysis.signal if analysis else "not run"
        lines.append(
            f"| {candidate.rank} | {candidate.ticker} | {candidate.score:.2f} | "
            f"{candidate.close:.2f} | {candidate.momentum_3m:.1%} | "
            f"{candidate.momentum_6m:.1%} | {candidate.momentum_12m:.1%} | "
            f"{candidate.distance_above_sma200:.1%} | {signal} |"
        )
    lines.extend(["", f"## Rejected or unavailable ({len(result.rejected)})", ""])
    if result.rejected:
        lines.extend(["| Ticker | Reason |", "|---|---|"])
        for rejection in result.rejected:
            reason = rejection.reason.replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {rejection.ticker} | {reason} |")
    else:
        lines.append("None.")
    if any(item.error for item in result.analyses):
        lines.extend(["", "## Agent analysis errors", ""])
        for analysis in result.analyses:
            if analysis.error:
                lines.append(f"- `{analysis.ticker}`: {analysis.error}")
    lines.append("")
    summary_path = destination / "summary.md"
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    return summary_path
