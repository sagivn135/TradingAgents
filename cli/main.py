import sys
from dataclasses import replace
from datetime import date as calendar_date, datetime
from pathlib import Path

import typer

from cli.display import console
from cli.run import run_analysis
from tradingagents.backtest import iter_grid, run_backtest, summarize
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.discovery import (
    DEFAULT_UNIVERSE_PATH,
    ScreenConfig,
    analyze_top_candidates,
    discover_stocks,
    load_universe,
    parse_tickers,
    write_discovery_report,
)
from tradingagents.portfolio import load_portfolio

# prompt_toolkit's win32 output module is importable only on Windows (it asserts
# the platform at import time), so gate on the platform rather than catching the
# failure — that way a genuinely broken prompt_toolkit on Windows still surfaces
# instead of silently disabling the handler below. Off Windows this stays an
# empty tuple, which `except` accepts and never matches (#1138).
if sys.platform == "win32":  # pragma: no cover - platform dependent
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError

    _NO_CONSOLE_ERRORS: tuple[type[BaseException], ...] = (NoConsoleScreenBufferError,)
else:
    _NO_CONSOLE_ERRORS = ()

app = typer.Typer(
    name="TradingAgents",
    help="TradingAgents CLI: Multi-Agents LLM Financial Trading Framework",
    add_completion=True,  # Enable shell completion
)


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable/disable checkpoint-resume (save state after each node so a "
        "crashed run can resume). Omit to honor TRADINGAGENTS_CHECKPOINT_ENABLED.",
    ),
    clear_checkpoints: bool = typer.Option(
        False,
        "--clear-checkpoints",
        help="Delete all saved checkpoints before running (force fresh start).",
    ),
    portfolio: str = typer.Option(
        None,
        "--portfolio",
        help="JSON file with current holdings and cash, so the trader, risk and "
        "portfolio agents size against your actual position.",
    ),
):
    """Run an analysis. This is what a bare `tradingagents` does."""
    if ctx.invoked_subcommand is not None:
        return
    if clear_checkpoints:
        from tradingagents.graph.checkpointer import clear_all_checkpoints
        n = clear_all_checkpoints(DEFAULT_CONFIG["data_cache_dir"])
        console.print(f"[yellow]Cleared {n} checkpoint(s).[/yellow]")
    portfolio_context = None
    if portfolio:
        try:
            portfolio_context = load_portfolio(portfolio)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from None

    try:
        run_analysis(checkpoint=checkpoint, portfolio=portfolio_context)
    except _NO_CONSOLE_ERRORS:
        # A terminal with no console buffer cannot host the interactive prompts.
        # Emit one actionable line on stderr instead of a prompt_toolkit
        # traceback; plain text, since rich may not render here either (#1138).
        typer.echo(
            "Error: no Windows console available. The interactive CLI needs a real "
            "console buffer — run it from Windows Terminal, PowerShell, or cmd.exe "
            "rather than a piped or embedded terminal.",
            err=True,
        )
        raise typer.Exit(code=1) from None


@app.command()
def backtest(
    tickers: str = typer.Argument(..., help="Comma-separated tickers, e.g. NVDA,AAPL"),
    start: str = typer.Option(..., "--start", help="First analysis date, YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="Last analysis date, YYYY-MM-DD"),
    every: int = typer.Option(7, "--every", help="Days between analysis dates"),
    analysts: str = typer.Option(
        None, "--analysts", help="Comma-separated analysts to run; omit for all four"
    ),
    asset_type: str = typer.Option("stock", "--asset-type", help="stock or crypto"),
    portfolio: str = typer.Option(
        None, "--portfolio", help="JSON file with holdings and cash, held constant across the grid"
    ),
    run_id: str = typer.Option(
        None, "--run-id", help="Continue an earlier sweep: its cells are skipped and its log reused"
    ),
):
    """Score past decisions over a grid of tickers and dates."""

    try:
        dates = iter_grid(start, end, every)
        book = load_portfolio(portfolio) if portfolio else None
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    names = [t.strip() for t in tickers.split(",") if t.strip()]
    if not names:
        console.print("[red]No ticker to analyze; pass them comma-separated, e.g. NVDA,AAPL[/red]")
        raise typer.Exit(code=1)

    kwargs = {"asset_type": asset_type, "portfolio": book, "run_id": run_id}
    if analysts:
        kwargs["selected_analysts"] = [a.strip().lower() for a in analysts.split(",") if a.strip()]

    try:
        result = run_backtest(names, dates, DEFAULT_CONFIG, **kwargs)
    except Exception as exc:  # a missing key or an unknown analyst is a setup error
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None
    console.print(summarize(result).render())
    console.print(f"\nRan {result.cells_run} cells, skipped {result.skipped}. Log: {result.log_path}")
    for ticker, failed_date, reason in result.failures:
        console.print(f"[yellow]failed:[/yellow] {ticker} {failed_date}: {reason}")
    for ticker, reason in result.settlement_failures:
        console.print(f"[yellow]unsettled:[/yellow] {ticker}: {reason}")


@app.command()
def discover(
    as_of: str = typer.Option(None, "--as-of", help="Point-in-time cutoff, YYYY-MM-DD"),
    tickers: str = typer.Option(
        None, "--tickers", help="Comma-separated universe; overrides --universe"
    ),
    universe: str = typer.Option(
        str(DEFAULT_UNIVERSE_PATH), "--universe", help="Text file with one ticker per line"
    ),
    analyze_top: int = typer.Option(
        0, "--analyze-top", min=0, help="Run TradingAgents for this many top-ranked stocks"
    ),
    analysts: str = typer.Option(
        "market,news,fundamentals",
        "--analysts",
        help="Analysts used by --analyze-top",
    ),
    min_price: float = typer.Option(5.0, "--min-price", min=0.0),
    min_dollar_volume: float = typer.Option(
        20_000_000.0, "--min-dollar-volume", min=0.0, help="Minimum 20-day average"
    ),
    workers: int = typer.Option(4, "--workers", min=1, max=16),
    checkpoint: bool | None = typer.Option(
        None,
        "--checkpoint/--no-checkpoint",
        help="Enable checkpoint-resume for --analyze-top",
    ),
    output: str = typer.Option(None, "--output", help="Output directory"),
):
    """Rank a stock universe, then optionally research the leading candidates."""
    cutoff = as_of or calendar_date.today().isoformat()
    try:
        if tickers:
            names = parse_tickers(tickers)
            universe_metadata = {
                "source": "CLI --tickers",
                "observed_at": datetime.now().astimezone().isoformat(),
            }
        else:
            names, universe_metadata = load_universe(universe)
        settings = replace(
            ScreenConfig(),
            min_price=min_price,
            min_avg_dollar_volume=min_dollar_volume,
            workers=workers,
        )
        result = discover_stocks(
            names,
            cutoff,
            config=settings,
            universe_source=universe_metadata["source"],
            universe_observed_at=universe_metadata.get("observed_at", "unknown"),
        )
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output) if output else Path(DEFAULT_CONFIG["results_dir"]) / "discovery" / stamp
    if analyze_top:
        selected = [name.strip().lower() for name in analysts.split(",") if name.strip()]
        graph_config = dict(DEFAULT_CONFIG)
        if checkpoint is not None:
            graph_config["checkpoint_enabled"] = checkpoint
        console.print(
            f"Screened {len(names)} symbols; analyzing the top "
            f"{min(analyze_top, len(result.candidates))} with TradingAgents..."
        )
        try:
            analyze_top_candidates(
                result,
                analyze_top,
                destination,
                selected_analysts=selected,
                graph_config=graph_config,
            )
        except Exception as exc:
            console.print(f"[red]Agent analysis could not start: {exc}[/red]")
            raise typer.Exit(code=1) from None
    summary_path = write_discovery_report(result, destination)

    console.print(
        f"Eligible: {len(result.candidates)}; rejected/unavailable: {len(result.rejected)}"
    )
    for candidate in result.candidates[:10]:
        analysis = next(
            (item.signal for item in result.analyses if item.ticker == candidate.ticker),
            "screen only",
        )
        console.print(
            f"#{candidate.rank:>2} {candidate.ticker:<8} score {candidate.score:>6.2f} "
            f"6m {candidate.momentum_6m:+.1%}  agent: {analysis}"
        )
    console.print(f"Report: {summary_path}")


if __name__ == "__main__":
    app()
