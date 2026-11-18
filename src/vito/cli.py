"""Command line interface.

Commands
--------
``vito status``   market context + account snapshot (read-only)
``vito policy``   the risk constitution and its digest
``vito doctor``   environment, connectivity, and credential checks
``vito net``      egress IP and allowlist drift
``vito backtest`` a strategy over historical candles, with costs
``vito walkforward`` parameters chosen on the past, scored on the future

Nothing here can move funds. Execution is a separate, armed object.
"""

from __future__ import annotations

import argparse
import getpass
import json
import platform
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from vito import __version__
from vito.backtest import buy_and_hold, run_backtest
from vito.backtest.panel import DEFAULT_BASKET, run_panel_momentum
from vito.backtest.walkforward import fold_table, oos_buy_and_hold, walk_forward
from vito.config import (
    Credentials,
    load_credentials,
    load_settings,
    repo_root,
    write_env_credentials,
)
from vito.data.instruments import scan_instruments
from vito.data.market import MarketRow, market_rows
from vito.data.ohlcv import (
    TIMEFRAME_MS,
    closed_only,
    fetch_candles,
    fetch_funding,
    fetch_recent,
)
from vito.exchange import ExchangeUnavailable, MissingCredentials, build_gateway
from vito.exec import build_shadow_plan
from vito.network import EgressStatus, check_egress, fetch_egress_ip
from vito.paper import PaperBroker, PaperPanelBroker, load_state
from vito.policy import Policy, PolicyError, load_policy
from vito.portfolio.snapshot import Snapshot, build_snapshot
from vito.risk import kill_switch_engaged
from vito.signals import (
    FeatureStore,
    build_panel_overlay_fn,
    calendar_digest,
    classify_fear_greed,
    evaluate_overlay,
    events_within_window,
    fear_greed_at,
    fetch_fear_greed,
    fetch_funding_panel,
    funding_percentile_at,
    latest_funding_rate_at,
    next_macro_event,
)
from vito.strategy import STRATEGIES, build

console = Console()


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------


def _money(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}"


def _signed_pct(value: float) -> str:
    colour = "green" if value > 0 else "red" if value < 0 else "white"
    return f"[{colour}]{value:+.2f}%[/{colour}]"


def _funding_pct(value: float) -> str:
    colour = "red" if value > 0 else "green" if value < 0 else "white"
    return f"[{colour}]{value:+.4f}%[/{colour}]"


def _when(ms: int) -> str:
    if not ms:
        return "—"
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def _header(policy: Policy) -> Panel:
    text = (
        f"[bold]Vito[/bold] {__version__}   "
        f"mode [bold]{policy.execution.mode}[/bold]   "
        f"policy [bold]{policy.version}[/bold]   "
        f"digest [bold cyan]{policy.digest()}[/bold cyan]"
    )
    return Panel(text, border_style="cyan", padding=(0, 1))


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _market_table(rows: list[MarketRow]) -> Table:
    table = Table(title="Market", title_justify="left", header_style="bold")
    table.add_column("Instrument", no_wrap=True)
    table.add_column("Last", justify="right")
    table.add_column("24h", justify="right")
    table.add_column("Spread", justify="right")
    table.add_column("Funding", justify="right")
    table.add_column("APR", justify="right")
    table.add_column("Next", justify="right", no_wrap=True)

    for row in rows:
        decimals = 2 if row.last >= 100 else 4
        table.add_row(
            row.inst_id,
            _money(row.last, decimals),
            _signed_pct(row.change_24h_pct),
            f"{row.spread_pct:.3f}%",
            _funding_pct(row.funding_rate_pct),
            _signed_pct(row.funding_apr_pct),
            _when(row.next_funding_ms),
        )
    if not rows:
        table.add_row("no data", "—", "—", "—", "—", "—", "—")
    return table


def _account_table(snapshot: Snapshot) -> Table:
    table = Table(title="Account", title_justify="left", header_style="bold")
    table.add_column("Currency")
    table.add_column("Free", justify="right")
    table.add_column("In use", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Value (USDT)", justify="right")
    table.add_column("Share", justify="right")

    for holding in snapshot.holdings:
        share = holding.usdt_value / snapshot.total_usdt * 100 if snapshot.total_usdt else 0
        value = _money(holding.usdt_value) if holding.priced else "[yellow]unpriced[/yellow]"
        table.add_row(
            holding.currency,
            _money(holding.free, 6),
            _money(holding.used, 6),
            _money(holding.total, 6),
            value,
            f"{share:.1f}%",
        )
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        "",
        "",
        "",
        f"[bold]{_money(snapshot.total_usdt)}[/bold]",
        f"{snapshot.stable_share_pct:.1f}% cash",
    )
    return table


def _guards_table(policy: Policy, halted: bool) -> Table:
    r = policy.risk
    lim = policy.limits
    table = Table(title="Guards", title_justify="left", header_style="bold")
    table.add_column("Control")
    table.add_column("Setting", justify="right")

    status = "[red]ENGAGED[/red]" if halted else "[green]clear[/green]"
    table.add_row("Kill switch", status)
    table.add_row(
        "Risk per trade",
        f"{r.risk_per_trade_pct:.2f}%  ({r.conviction_risk_pct:.2f}% conviction)",
    )
    table.add_row("Max leverage", f"{r.max_leverage:.1f}x")
    table.add_row("Max gross exposure", f"{r.max_gross_exposure_mult:.1f}x equity")
    table.add_row("Max open positions", str(r.max_open_positions))
    table.add_row("Daily loss halt", f"{lim.daily_loss_halt_pct:.1f}%")
    table.add_row("Peak drawdown stop", f"{lim.peak_drawdown_stop_pct:.1f}%")
    return table


def cmd_status(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    settings = load_settings(repo_root())
    credentials = load_credentials()
    gateway = build_gateway(credentials)
    halted = kill_switch_engaged(policy.execution.kill_switch_file)

    try:
        rows = market_rows(gateway, settings.symbols)
    except ExchangeUnavailable as exc:
        console.print(f"[red]Exchange unreachable:[/red] {exc}")
        return 2

    snapshot: Snapshot | None = None
    account_error: str | None = None
    if gateway.has_credentials:
        try:
            snapshot = build_snapshot(gateway)
        except MissingCredentials:  # pragma: no cover - guarded above
            account_error = "credentials incomplete"
        except ExchangeUnavailable as exc:
            account_error = str(exc)

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "version": __version__,
                    "mode": policy.execution.mode,
                    "policy": {"version": policy.version, "digest": policy.digest()},
                    "kill_switch": halted,
                    "market": [asdict(row) for row in rows],
                    "account": None
                    if snapshot is None
                    else {
                        "total_usdt": snapshot.total_usdt,
                        "cash_usdt": snapshot.cash_usdt,
                        "holdings": [asdict(h) for h in snapshot.holdings],
                    },
                    "account_error": account_error,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    console.print(_market_table(rows))

    if snapshot is not None:
        console.print(_account_table(snapshot))
    else:
        reason = account_error or "no API credentials configured"
        console.print(
            Panel(
                f"[yellow]{reason}[/yellow]\n\n"
                "Read-only account access:\n"
                "  1. create a [bold]sub-account[/bold] on Binance with only your risk capital\n"
                "  2. API key with [bold]Reading[/bold] only, withdraw disabled,\n"
                "     IP allowlist enabled\n"
                "  3. run [bold]vito setup[/bold] (hidden input, writes mode-0600 .env)\n"
                "  4. re-run [bold]vito status[/bold]",
                title="Account",
                title_align="left",
                border_style="yellow",
            )
        )

    console.print(_guards_table(policy, halted))
    return 0


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def _policy_rows(policy: Policy) -> list[tuple[str, str, str]]:
    r, lim, ex = policy.risk, policy.limits, policy.execution
    return [
        ("version", policy.version, "policy schema"),
        ("digest", policy.digest(), "changes whenever any value below changes"),
        ("equity_floor_usdt", _money(policy.equity_floor_usdt), "below this, no new positions"),
        ("risk_per_trade_pct", f"{r.risk_per_trade_pct:.2f}%", "loss at stop, per trade"),
        ("conviction_risk_pct", f"{r.conviction_risk_pct:.2f}%", "BTC only"),
        ("max_leverage", f"{r.max_leverage:.1f}x", "ceiling, usually non-binding"),
        ("max_gross_exposure_mult", f"{r.max_gross_exposure_mult:.1f}x", "gap-risk limit"),
        ("max_open_positions", str(r.max_open_positions), "concentration limit"),
        ("min_stop_distance_pct", f"{r.min_stop_distance_pct:.2f}%", "tighter than this is noise"),
        (
            "liquidation_buffer_mult",
            f"{r.liquidation_buffer_mult:.1f}x",
            "liq must be this many stops away",
        ),
        ("max_slippage_pct", f"{r.max_slippage_pct:.2f}%", "reject fills worse than this"),
        ("funding_guard_minutes", str(r.funding_guard_minutes), "no entry near funding"),
        ("daily_loss_halt_pct", f"{lim.daily_loss_halt_pct:.1f}%", "rolling 24h halt"),
        ("peak_drawdown_stop_pct", f"{lim.peak_drawdown_stop_pct:.1f}%", "manual restart required"),
        (
            "loss_streak_cooldown",
            str(lim.loss_streak_cooldown),
            f"then {lim.cooldown_hours}h pause",
        ),
        ("instruments.allowed", ", ".join(policy.instruments.allowed), "whitelist"),
        ("conviction_allowed", ", ".join(policy.instruments.conviction_allowed), "conviction tier"),
        ("execution.mode", ex.mode, "readonly | paper | live"),
        ("kill_switch_file", ex.kill_switch_file, "touch this file to halt"),
    ]


def cmd_policy(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)

    if args.json:
        console.print_json(json.dumps(policy.as_dict(), default=str))
        return 0

    table = Table(title=f"Risk policy v{policy.version}", title_justify="left", header_style="bold")
    table.add_column("Parameter", no_wrap=True)
    table.add_column("Value", justify="right")
    table.add_column("Meaning", style="dim")
    for name, value, meaning in _policy_rows(policy):
        table.add_row(name, value, meaning)
    console.print(table)
    console.print(
        f"digest [bold cyan]{policy.digest()}[/bold cyan] — type this to arm live routing"
    )
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def _check(name: str, ok: bool | None, detail: str) -> tuple[str, str, str]:
    mark = "[green]✓[/green]" if ok else "[red]✗[/red]" if ok is False else "[yellow]–[/yellow]"
    return mark, name, detail


def cmd_doctor(args: argparse.Namespace) -> int:
    checks: list[tuple[str, str, str]] = []
    failures = 0

    version_ok = sys.version_info >= (3, 11)
    checks.append(
        _check("python", version_ok, platform.python_version() + " (need >= 3.11)")
    )
    failures += not version_ok

    try:
        import ccxt

        checks.append(_check("ccxt", True, ccxt.__version__))
    except ImportError:  # pragma: no cover
        checks.append(_check("ccxt", False, "not installed"))
        failures += 1

    try:
        policy = load_policy(args.policy)
        checks.append(_check("policy", True, f"v{policy.version} · {policy.digest()}"))
    except PolicyError as exc:
        checks.append(_check("policy", False, str(exc).splitlines()[0]))
        failures += 1
        policy = None

    credentials = load_credentials()
    checks.append(
        _check(
            "credentials",
            None if not credentials.complete else True,
            f"{credentials.masked()}"
            + (" · demo" if credentials.demo else "")
            + ("" if credentials.complete else " · public data only"),
        )
    )

    gateway = build_gateway(credentials)
    reachable = gateway.ping()
    venue_label = getattr(gateway, "venue", "binance")
    checks.append(
        _check(f"{venue_label} reachable", reachable, "api ok" if reachable else "no response")
    )
    failures += not reachable

    settings = load_settings(repo_root())
    egress = fetch_egress_ip()
    status = check_egress(settings.expected_egress_ip, egress.ip)
    if status is EgressStatus.MATCH:
        checks.append(_check("egress ip", True, f"{egress.ip} matches allowlist"))
    elif status is EgressStatus.MISMATCH:
        checks.append(
            _check(
                "egress ip",
                False,
                f"{egress.ip} != {settings.expected_egress_ip} — update the OKX allowlist",
            )
        )
        failures += 1
    elif status is EgressStatus.UNCONFIGURED:
        checks.append(
            _check("egress ip", None, f"{egress.ip or 'unknown'} · allowlist not configured")
        )
    else:
        checks.append(_check("egress ip", False, egress.error or "unreachable"))
        failures += 1

    if policy is not None:
        halt_path = Path(policy.execution.kill_switch_file).expanduser()
        engaged = halt_path.exists()
        checks.append(
            _check(
                "kill switch",
                None if engaged else True,
                f"ENGAGED at {halt_path}" if engaged else "clear",
            )
        )
        audit_parent = Path(policy.execution.audit_log).expanduser().parent
        try:
            audit_parent.mkdir(parents=True, exist_ok=True)
            probe = audit_parent / ".vito-write-test"
            probe.write_text("ok")
            probe.unlink()
            checks.append(_check("audit log", True, str(audit_parent)))
        except OSError as exc:
            checks.append(_check("audit log", False, str(exc)))
            failures += 1

    table = Table(title="Doctor", title_justify="left", header_style="bold")
    table.add_column("")
    table.add_column("Check")
    table.add_column("Detail", style="dim")
    for row in checks:
        table.add_row(*row)
    console.print(_header(policy) if policy else Panel(f"Vito {__version__}", border_style="cyan"))
    console.print(table)

    return 1 if failures else 0


def cmd_setup(args: argparse.Namespace) -> int:
    """Prompt for exchange credentials with echo disabled and write mode-0600 `.env`."""
    venue = str(getattr(args, "venue", "binance") or "binance").lower()
    venue_title = venue.upper()
    console.print(
        Panel(
            f"Enter your [bold]{venue_title}[/bold] sub-account API credentials.\n"
            "Input is hidden (nothing is echoed to the terminal or shell history).",
            title="Vito Setup",
            title_align="left",
            border_style="cyan",
        )
    )
    api_key = getpass.getpass(f"{venue_title} API Key (hidden): ").strip()
    api_secret = getpass.getpass(f"{venue_title} API Secret (hidden): ").strip()
    passphrase = (
        getpass.getpass("OKX Passphrase (hidden): ").strip() if venue == "okx" else ""
    )

    creds = Credentials(
        api_key=api_key,
        api_secret=api_secret,
        passphrase=passphrase,
        demo=bool(args.demo),
        venue=venue,
    )
    if not creds.complete:
        console.print("[red]setup aborted: required credential fields were empty[/red]")
        return 1

    env_path = write_env_credentials(
        api_key, api_secret, passphrase, demo=bool(args.demo), venue=venue
    )
    console.print(
        f"[green]✓[/green] wrote [cyan]{env_path}[/cyan] (mode 0600) · "
        f"venue [bold]{venue}[/bold] · key [bold]{creds.masked()}[/bold]"
    )

    gateway = build_gateway(creds)
    try:
        snapshot = build_snapshot(gateway)
    except ExchangeUnavailable as exc:
        console.print(
            f"[yellow]saved .env, but {venue_title} rejected the check:[/yellow] {exc}"
        )
        return 2

    console.print(
        f"[green]✓[/green] authenticated with {venue_title} · "
        f"total equity [bold]{_money(snapshot.total_usdt)}[/bold] USDT"
    )
    return 0


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())

    with console.status(f"fetching {args.symbol} {args.timeframe}…", spinner="dots"):
        candles = fetch_candles(
            gateway, args.symbol, timeframe=args.timeframe, days=args.days, refresh=args.refresh
        )
        funding = None
        if args.symbol.endswith("-SWAP"):
            funding = fetch_funding(
                gateway, args.symbol, days=args.days, refresh=args.refresh
            )
        try:
            min_notional = gateway.min_notional(args.symbol)
        except ExchangeUnavailable:
            min_notional = 0.0

    if candles.empty:
        console.print("[red]no candles returned[/red]")
        return 2

    span_start = datetime.fromtimestamp(int(candles["timestamp"].iloc[0]) / 1000)
    span_end = datetime.fromtimestamp(int(candles["timestamp"].iloc[-1]) / 1000)

    names = list(STRATEGIES) if args.strategy == "all" else [args.strategy]
    results: dict[str, dict] = {}

    for name in names:
        signals = build(name).signals(args.symbol, candles)
        result = run_backtest(
            candles,
            signals,
            inst_id=args.symbol,
            strategy_name=name,
            policy=policy,
            initial_equity=args.initial,
            fee_bps=args.fee_bps,
            funding=funding,
            timeframe=args.timeframe,
            min_notional=min_notional,
        )
        results[name] = {**result.metrics, "skipped": result.skipped}

    benchmark = buy_and_hold(candles, initial_equity=args.initial, timeframe=args.timeframe)

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "symbol": args.symbol,
                    "timeframe": args.timeframe,
                    "bars": len(candles),
                    "from": span_start.isoformat(),
                    "to": span_end.isoformat(),
                    "reduce_only": True,
                    "strategies": results,
                    "buy_and_hold": benchmark,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    console.print(
        f"[bold]{args.symbol}[/bold] {args.timeframe} · {len(candles)} bars · "
        f"{span_start:%Y-%m-%d} → {span_end:%Y-%m-%d} · "
        f"start {_money(args.initial)} USDT"
    )

    table = Table(title="Backtest", title_justify="left", header_style="bold")
    for column in ("Strategy", "Return", "CAGR", "Max DD", "Sharpe", "Trades", "Win", "PF", "ExpR"):
        table.add_column(column, justify="right" if column != "Strategy" else "left")

    for name, m in results.items():
        table.add_row(
            name,
            _signed_pct(float(m["total_return_pct"])),
            _signed_pct(float(m["cagr_pct"])),
            f"[red]{float(m['max_drawdown_pct']):.1f}%[/red]",
            f"{float(m['sharpe']):.2f}",
            str(int(m["trades"])),
            f"{float(m['win_rate_pct']):.0f}%",
            f"{float(m['profit_factor']):.2f}",
            f"{float(m['expectancy_r']):+.2f}",
        )
    table.add_section()
    table.add_row(
        "[dim]buy & hold[/dim]",
        f"[dim]{float(benchmark['total_return_pct']):+.2f}%[/dim]",
        f"[dim]{float(benchmark['cagr_pct']):+.2f}%[/dim]",
        f"[dim]{float(benchmark['max_drawdown_pct']):.1f}%[/dim]",
        f"[dim]{float(benchmark['sharpe']):.2f}[/dim]",
        "[dim]—[/dim]",
        "[dim]—[/dim]",
        "[dim]—[/dim]",
        "[dim]—[/dim]",
    )
    console.print(table)

    for name, m in results.items():
        skipped = m.get("skipped") or {}
        if skipped:
            detail = ", ".join(f"{k}×{v}" for k, v in sorted(skipped.items()))
            console.print(f"[dim]{name}: refused by policy — {detail}[/dim]")

    console.print(
        "[dim]fees "
        + " · ".join(
            f"{n} {_money(float(m['fees_paid']))}" for n, m in results.items()
        )
        + " USDT · funding "
        + " · ".join(
            f"{n} {_money(float(m['funding_paid']))}" for n, m in results.items()
        )
        + " USDT[/dim]"
    )
    return 0


# ---------------------------------------------------------------------------
# walk-forward
# ---------------------------------------------------------------------------


def cmd_walkforward(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())

    with console.status(f"fetching {args.symbol} {args.timeframe}…", spinner="dots"):
        candles = fetch_candles(
            gateway, args.symbol, timeframe=args.timeframe, days=args.days, refresh=args.refresh
        )
        funding = None
        if args.symbol.endswith("-SWAP"):
            funding = fetch_funding(gateway, args.symbol, days=args.days, refresh=args.refresh)
        try:
            min_notional = gateway.min_notional(args.symbol)
        except ExchangeUnavailable:
            min_notional = 0.0

    if candles.empty:
        console.print("[red]no candles returned[/red]")
        return 2

    try:
        result = walk_forward(
            candles,
            inst_id=args.symbol,
            strategy_name=args.strategy,
            policy=policy,
            train_bars=args.train_bars,
            test_bars=args.test_bars,
            initial_equity=args.initial,
            fee_bps=args.fee_bps,
            funding=funding,
            timeframe=args.timeframe,
            objective=args.objective,
            min_train_trades=args.min_train_trades,
            min_train_score=args.min_train_score,
            min_notional=min_notional,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    benchmark = oos_buy_and_hold(candles, result, initial_equity=args.initial)

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "symbol": args.symbol,
                    "timeframe": args.timeframe,
                    "strategy": args.strategy,
                    "objective": args.objective,
                    "train_bars": args.train_bars,
                    "test_bars": args.test_bars,
                    "folds": fold_table(result),
                    "out_of_sample": result.metrics,
                    "buy_and_hold": benchmark,
                    "selection_stability": result.selection,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    console.print(
        f"[bold]{args.symbol}[/bold] {args.timeframe} · {args.strategy} · "
        f"train {args.train_bars} / test {args.test_bars} bars · "
        f"objective {args.objective} · start {_money(args.initial)} USDT"
    )

    table = Table(title="Walk-forward", title_justify="left", header_style="bold")
    for column in ("#", "Chosen on train", "Test", "Tr", "Score", "Trd"):
        table.add_column(column, justify="left" if column == "Chosen on train" else "right")

    for row in fold_table(result):
        ret = float(row["test_return_pct"])
        table.add_row(
            str(row["fold"]),
            row["params"],
            f"[{'green' if ret > 0 else 'red'}]{ret:+.2f}%[/]",
            str(row["test_trades"]),
            f"{float(row['train_score']):+.2f}",
            str(row["train_trades"]),
        )
    console.print(table)

    m = result.metrics
    console.print(
        f"out of sample · {int(m['folds'])} folds ({int(m['folds_traded'])} traded) · "
        f"{int(m['trades'])} trades · return [bold]{m['total_return_pct']:+.2f}%[/bold] · "
        f"max DD [red]{m['max_drawdown_pct']:.1f}%[/red] · sharpe {m['sharpe']:.2f} "
        f"(DSR {m.get('deflated_sharpe', 0.0):.2f}, t={m.get('t_stat', 0.0):+.2f}) · "
        f"fees {m['fees_paid']:.2f} USDT"
    )
    console.print(
        f"[dim]buy & hold over the same span · {benchmark['total_return_pct']:+.2f}% · "
        f"DD {benchmark['max_drawdown_pct']:.1f}% · sharpe {benchmark['sharpe']:.2f}[/dim]"
    )
    if result.selection:
        stability = " · ".join(f"{k} {v:.0%}" for k, v in result.selection.items())
        console.print(
            "[dim]parameter stability (share of folds picking the modal value): "
            f"{stability}[/dim]"
        )

    positives = int(m["folds_positive"])
    if positives * 2 <= int(m["folds"]):
        console.print(
            "[yellow]fewer than half the folds were profitable out of sample — "
            "the in-sample result did not survive[/yellow]"
        )
    return 0


# ---------------------------------------------------------------------------
# paper
# ---------------------------------------------------------------------------


def _parse_params(values: list[str] | None) -> dict[str, object]:
    out: dict[str, object] = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--param expects key=value, got {item!r}")
        key, raw = item.split("=", 1)
        try:
            out[key] = int(raw)
        except ValueError:
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = raw
    return out


def cmd_paper_step(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())

    with console.status(f"fetching {args.symbol} {args.timeframe}…", spinner="dots"):
        raw = fetch_recent(gateway, args.symbol, timeframe=args.timeframe, limit=args.bars)
        candles = closed_only(raw, args.timeframe)

    if candles.empty:
        console.print("[red]no closed candles returned[/red]")
        return 2

    broker = PaperBroker(
        policy=policy,
        inst_id=args.symbol,
        strategy=args.strategy,
        timeframe=args.timeframe,
        initial_equity=args.initial,
        fee_bps=args.fee_bps,
        params=_parse_params(args.param),
        state_path=Path(args.state).expanduser() if args.state else None,
    )
    if args.reset:
        broker.reset()

    report = broker.step(candles)
    summary = report.state.summary()

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "status": report.status,
                    "bars_processed": report.bars_processed,
                    "actions": list(report.actions),
                    "marked_equity": round(report.marked_equity, 2),
                    "state_file": str(broker.state_path),
                    **summary,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    console.print(
        f"[bold]{args.symbol}[/bold] {args.timeframe} · {args.strategy} · "
        f"paper · start {_money(args.initial)} USDT"
    )

    if report.status == "warmed_up":
        console.print(
            "[dim]warmed up on history — no position taken. The next closed bar "
            "is the first one that can trade.[/dim]"
        )
    elif report.status == "no_new_bar":
        console.print("[dim]no new closed bar since the last step[/dim]")
    else:
        console.print(f"[dim]{report.bars_processed} new bar(s) processed[/dim]")

    for action in report.actions:
        colour = "green" if action.startswith("open") else "yellow"
        console.print(f"  [{colour}]{action}[/{colour}]")

    if report.state.position:
        pos = report.state.position
        console.print(
            f"  open {pos['side']} {pos['notional']:.2f} USDT @ "
            f"{pos['entry_price']:.2f} · stop {pos['stop']:.2f}"
        )

    console.print(
        f"equity [bold]{_money(summary['equity'])}[/bold] USDT · "
        f"{summary['return_pct']:+.2f}% · {summary['trades']} trades · "
        f"fees {summary['fees_paid']:.2f}"
    )
    console.print(f"[dim]state: {broker.state_path}[/dim]")
    return 0


def cmd_paper_status(args: argparse.Namespace) -> int:
    if args.state:
        paths = [Path(args.state).expanduser()]
    else:
        paths = sorted(Path("~/.vito").expanduser().glob("paper-*.json"))

    if not paths:
        console.print("[dim]no paper runs yet — start one with `vito paper step`[/dim]")
        return 0

    rows = []
    for path in paths:
        state = load_state(path)
        if state is None:
            continue
        summary = state.summary()
        rows.append({"file": path.name, **summary})

    if args.json:
        console.print_json(json.dumps(rows, default=str))
        return 0

    table = Table(title="Paper runs", title_justify="left", header_style="bold")
    for column in ("Run", "Equity", "Return", "Trades", "Win", "Fees", "Position"):
        table.add_column(column, justify="left" if column == "Run" else "right")

    for row in rows:
        position = row["position"]["side"] if row.get("position") else "flat"
        table.add_row(
            f"{row['inst_id']} {row['strategy']} {row['timeframe']}",
            _money(float(row["equity"])),
            _signed_pct(float(row["return_pct"])),
            str(int(row["trades"])),
            f"{float(row['win_rate_pct']):.0f}%",
            f"{float(row['fees_paid']):.2f}",
            position,
        )
    console.print(table)
    return 0


def cmd_paper_panel(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    frames = {}
    with console.status(f"fetching {len(symbols)} instruments ({args.timeframe})…", spinner="dots"):
        for sym in symbols:
            try:
                raw = fetch_recent(gateway, sym, timeframe=args.timeframe, limit=args.bars)
                closed = closed_only(raw, args.timeframe)
                if not closed.empty:
                    frames[sym] = closed
            except ExchangeUnavailable:
                continue

    if not frames:
        console.print("[red]no closed candles returned for panel[/red]")
        return 2

    broker = PaperPanelBroker(
        policy=policy,
        symbols=symbols,
        timeframe=args.timeframe,
        lookback=args.lookback,
        rebalance_every=args.rebalance_every,
        max_positions=args.max_positions,
        max_exposure=args.max_exposure,
        risk_per_trade_pct=args.risk_pct,
        initial_equity=args.initial,
        fee_bps=args.fee_bps,
        state_path=Path(args.state).expanduser() if args.state else None,
    )
    if args.reset:
        broker.reset()

    report = broker.step(frames)
    summary = report.state.summary()
    pending = report.state.params.get("pending_targets") or []

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "status": report.status,
                    "bars_processed": report.bars_processed,
                    "actions": list(report.actions),
                    "pending_targets": pending,
                    "state_file": str(broker.state_path),
                    **summary,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    console.print(
        f"[bold]PANEL ({len(frames)} assets)[/bold] {args.timeframe} · momentum · "
        f"paper · start {_money(args.initial)} USDT"
    )
    if report.status == "warmed_up":
        targets = (
            ", ".join(f"{t['sym']} (score {t['score']:+.2f})" for t in pending)
            if pending
            else "cash (no uptrend)"
        )
        console.print(
            f"[dim]warmed up on history — queued for next open: {targets}[/dim]"
        )
    elif report.status == "no_new_bar":
        console.print("[dim]no new closed bar since the last step[/dim]")
    else:
        console.print(f"[dim]{report.bars_processed} new bar(s) processed[/dim]")

    for action in report.actions:
        console.print(f"  [green]{action}[/green]")

    console.print(
        f"equity [bold]{_money(summary['equity'])}[/bold] USDT · "
        f"{summary['return_pct']:+.2f}% · fees {summary['fees_paid']:.2f}"
    )
    console.print(f"[dim]state: {broker.state_path}[/dim]")
    return 0


# ---------------------------------------------------------------------------
# net
# ---------------------------------------------------------------------------


def cmd_net(args: argparse.Namespace) -> int:
    settings = load_settings(repo_root())
    egress = fetch_egress_ip()
    status = check_egress(settings.expected_egress_ip, egress.ip)

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "egress_ip": egress.ip,
                    "provider": egress.provider,
                    "expected": settings.expected_egress_ip or None,
                    "status": str(status),
                    "error": egress.error,
                }
            )
        )
    else:
        if egress.ok:
            console.print(
                f"egress ip  [bold]{egress.ip}[/bold]  [dim]({egress.provider})[/dim]"
            )
        else:
            console.print(f"egress ip  [red]unavailable[/red]  [dim]{egress.error}[/dim]")

        if status is EgressStatus.MATCH:
            console.print(f"allowlist  [green]match[/green]  {settings.expected_egress_ip}")
        elif status is EgressStatus.MISMATCH:
            console.print(
                f"allowlist  [red]drift[/red]  configured {settings.expected_egress_ip}, "
                f"actual {egress.ip}"
            )
            console.print(
                "[yellow]OKX will reject this key. Update the allowlist, or move to a "
                "static egress IP (docs/okx-setup.md).[/yellow]"
            )
        elif status is EgressStatus.UNCONFIGURED:
            console.print(
                "allowlist  [yellow]not configured[/yellow]  set "
                "\\[network].expected_egress_ip in config/config.toml"
            )
        else:
            console.print("allowlist  [red]unknown[/red]")

    if status is EgressStatus.MISMATCH:
        return 1
    if status is EgressStatus.UNREACHABLE:
        return 2
    return 0


# ---------------------------------------------------------------------------
# instruments
# ---------------------------------------------------------------------------


def cmd_instruments(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())

    with console.status("scanning exchange instruments…", spinner="dots"):
        specs = scan_instruments(
            gateway,
            kind=args.kind,
            asset_class=args.asset_class,
            equity=args.equity,
            max_exposure_mult=args.max_exposure,
            min_volume_usdt=args.min_vol,
            max_spread_bps=args.max_spread_bps,
            only_tradeable=not args.all,
        )

    shown = specs[: args.limit] if args.limit > 0 else specs

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "total_matching": len(specs),
                    "tradeable_count": sum(1 for s in specs if s.tradeable),
                    "gate": {
                        "equity_usdt": args.equity,
                        "max_exposure_mult": args.max_exposure,
                        "max_order_usdt": round(args.equity * args.max_exposure, 2),
                        "min_volume_usdt": args.min_vol,
                        "max_spread_bps": args.max_spread_bps,
                    },
                    "instruments": [asdict(s) for s in shown],
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    max_order = args.equity * args.max_exposure
    console.print(
        f"gate · equity {_money(args.equity)} USDT · max order {_money(max_order)} USDT "
        f"({args.max_exposure:.2f}x) · min 24h vol ${_money(args.min_vol, 0)} · "
        f"max spread {args.max_spread_bps:.1f} bps"
    )

    table = Table(
        title=f"Instruments ({len(shown)} of {len(specs)})",
        title_justify="left",
        header_style="bold",
    )
    for col in (
        "Instrument",
        "Class",
        "Kind",
        "Last",
        "Spread",
        "24h Vol $",
        "ctVal",
        "minSz",
        "Min Order $",
        "Taker",
        "Gate",
    ):
        table.add_column(
            col,
            justify="left" if col in ("Instrument", "Class", "Kind", "Gate") else "right",
            no_wrap=True,
        )

    for s in shown:
        decimals = 2 if s.last >= 10 else 4
        gate_str = (
            "[green]pass[/green]"
            if s.tradeable
            else f"[red]{s.reject_reason or 'reject'}[/red]"
        )
        table.add_row(
            s.inst_id,
            s.asset_class,
            s.kind,
            _money(s.last, decimals),
            f"{s.spread_bps:.1f}bp",
            _money(s.volume_24h_usdt, 0),
            f"{s.contract_size:g}",
            f"{s.min_size:g}",
            f"${_money(s.min_notional_usdt)}",
            f"{s.taker_bps:.1f}bp",
            gate_str,
        )
    if not shown:
        table.add_row("none", "—", "—", "—", "—", "—", "—", "—", "—", "—", "—")

    console.print(table)
    return 0


# ---------------------------------------------------------------------------
# panel
# ---------------------------------------------------------------------------


def cmd_panel(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    frames = {}
    min_notional: dict[str, float] = {}
    with console.status(f"fetching {len(symbols)} instruments ({args.timeframe})…", spinner="dots"):
        for sym in symbols:
            try:
                df = fetch_candles(
                    gateway, sym, timeframe=args.timeframe, days=args.days, refresh=args.refresh
                )
            except ExchangeUnavailable:
                continue
            if not df.empty:
                frames[sym] = df
            try:
                min_notional[sym] = gateway.min_notional(sym)
            except ExchangeUnavailable:
                min_notional[sym] = 0.0

    if not frames:
        console.print("[red]no candles returned for basket[/red]")
        return 2

    overlay_fn = None
    if getattr(args, "overlay", False):
        with console.status("fetching Fear & Greed + funding crowding history…", spinner="dots"):
            fng_series = fetch_fear_greed(refresh=args.refresh)
            fund_series = fetch_funding_panel(
                gateway, symbols, days=max(args.days + 365, 1460), refresh=args.refresh
            )
            overlay_fn = build_panel_overlay_fn(
                fng_series=fng_series,
                funding_series=fund_series,
                check_calendar=True,
            )

    try:
        result = run_panel_momentum(
            frames,
            timeframe=args.timeframe,
            lookback=args.lookback,
            rebalance_every=args.rebalance_every,
            max_positions=args.max_positions,
            max_per_group=getattr(args, "max_per_group", None),
            max_gross_exposure_mult=args.max_exposure,
            risk_per_trade_pct=args.risk_pct,
            initial_equity=args.initial,
            fee_bps=args.fee_bps,
            cash_apr_pct=args.cash_apr,
            min_notional=min_notional,
            overlay_fn=overlay_fn,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2

    span_start = result.equity_curve.index[result.lookback]
    span_end = result.equity_curve.index[-1]

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "symbols": list(result.symbols),
                    "timeframe": result.timeframe,
                    "lookback": result.lookback,
                    "rebalance_every": result.rebalance_every,
                    "from": span_start.isoformat(),
                    "to": span_end.isoformat(),
                    "metrics": result.metrics,
                    "equal_weight_benchmark": result.benchmark,
                    "asset_stats": result.asset_stats,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    console.print(
        f"[bold]Cross-Asset Momentum Panel[/bold] · {len(result.symbols)} instruments · "
        f"{result.timeframe} · lookback {result.lookback} / "
        f"rebalance {result.rebalance_every} bars · "
        f"risk {args.risk_pct:.1f}% · top {args.max_positions} @ {args.max_exposure:.2f}x gross · "
        f"{span_start:%Y-%m-%d} → {span_end:%Y-%m-%d}"
    )

    table = Table(title="Asset Contribution", title_justify="left", header_style="bold")
    table.add_column("Instrument")
    table.add_column("Bars Held", justify="right")
    table.add_column("Time Held", justify="right")
    table.add_column("Gross PnL (USDT)", justify="right")
    for sym, st in sorted(
        result.asset_stats.items(), key=lambda kv: -kv[1]["pnl_usdt"]
    ):
        pnl = float(st["pnl_usdt"])
        col = "green" if pnl > 0 else "red" if pnl < 0 else "white"
        table.add_row(
            sym,
            str(int(st["bars_held"])),
            f"{st['share_held_pct']:.1f}%",
            f"[{col}]{pnl:+.2f}[/{col}]",
        )
    console.print(table)

    m = result.metrics
    b = result.benchmark
    cy_note = (
        f" · earn +{m['cash_yield_usdt']:.2f} USDT"
        if m.get("cash_yield_usdt", 0.0) > 0
        else ""
    )
    console.print(
        f"portfolio · return [bold]{m['total_return_pct']:+.2f}%[/bold] "
        f"(CAGR {m['cagr_pct']:+.2f}%) · max DD [red]{m['max_drawdown_pct']:.1f}%[/red] · "
        f"sharpe {m['sharpe']:.2f} (t={m['t_stat']:+.2f}) · "
        f"stops {int(m.get('stops_hit', 0))} · "
        f"exposure {m['exposure_pct']:.1f}% · fees {m['fees_paid']:.2f} USDT{cy_note}"
    )
    if "overlay_blocked_entries" in m:
        console.print(
            f"[cyan]overlay · blocked entries {int(m['overlay_blocked_entries'])} "
            f"(avg 5d fwd {m['overlay_blocked_avg_5d_ret_pct']:+.2f}%) · "
            f"dampened allocations {int(m['overlay_scaled_allocs'])}[/cyan]"
        )
    console.print(
        f"[dim]equal-weight basket buy & hold · return {b['total_return_pct']:+.2f}% "
        f"(CAGR {b['cagr_pct']:+.2f}%) · max DD {b['max_drawdown_pct']:.1f}% · "
        f"sharpe {b['sharpe']:.2f} (t={b['t_stat']:+.2f})[/dim]"
    )
    return 0


def cmd_signals(args: argparse.Namespace) -> int:
    """Display live Tier-1 signals (Fear & Greed, funding crowding, FOMC/CPI calendar)."""
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    now_ms = int(time.time() * 1000)

    msg = "fetching Fear & Greed, macro calendar, and funding crowding…"
    with console.status(msg, spinner="dots"):
        fng_series = fetch_fear_greed(refresh=args.refresh)
        fg_val = fear_greed_at(fng_series, now_ms)
        fund_map = fetch_funding_panel(gateway, symbols, days=365, refresh=args.refresh)
        fund_pcts = {sym: funding_percentile_at(s, now_ms) for sym, s in fund_map.items()}
        fund_rates = {sym: latest_funding_rate_at(s, now_ms) for sym, s in fund_map.items()}
        ov = evaluate_overlay(
            now_ms,
            fear_greed=fg_val,
            funding_pct=fund_pcts,
            check_calendar=True,
        )
        next_ev = next_macro_event(now_ms)
        upcoming_24h = events_within_window(now_ms, hours_ahead=24.0)

    store = FeatureStore(Path("~/.vito/features.jsonl"))
    feat_hash = store.append(
        key="signals_snapshot",
        obs_ts=now_ms,
        value={
            "fear_greed": fg_val,
            "funding_pct": fund_pcts,
            "risk_scale": ov.risk_scale,
            "block_new_entries": ov.block_new_entries,
            "reasons": list(ov.reasons),
        },
        meta={"calendar_digest": calendar_digest()},
    )

    if args.json:
        console.print_json(
            json.dumps(
                {
                    "timestamp_ms": now_ms,
                    "fear_greed": fg_val,
                    "fear_greed_regime": (
                        classify_fear_greed(fg_val) if fg_val is not None else None
                    ),
                    "calendar_digest": calendar_digest(),
                    "next_macro_event": asdict(next_ev) if next_ev else None,
                    "upcoming_24h": [asdict(e) for e in upcoming_24h],
                    "funding_pct": fund_pcts,
                    "funding_rate_8h": fund_rates,
                    "overlay": {
                        "risk_scale": ov.risk_scale,
                        "symbol_scales": ov.symbol_scales,
                        "block_new_entries": ov.block_new_entries,
                        "reasons": list(ov.reasons),
                    },
                    "feature_hash": feat_hash,
                },
                default=str,
            )
        )
        return 0

    console.print(_header(policy))
    fg_str = (
        f"{fg_val:.0f}/100 ({classify_fear_greed(fg_val)})"
        if fg_val is not None
        else "unavailable"
    )
    if next_ev is not None:
        hours_away = (next_ev.ts_ms - now_ms) / 3_600_000.0
        ev_str = f"{next_ev.kind} at {next_ev.iso_utc} (in {hours_away:.1f}h)"
    else:
        ev_str = "none scheduled"

    console.print(
        f"[bold]Tier-1 Risk Signals[/bold] · Fear & Greed [cyan]{fg_str}[/cyan] · "
        f"next macro event [yellow]{ev_str}[/yellow] · calendar digest {calendar_digest()}"
    )

    table = Table(
        title="Perpetual Funding Crowding (Trailing 1-Year Percentile)",
        title_justify="left",
        header_style="bold",
    )
    table.add_column("Instrument")
    table.add_column("8h Rate", justify="right")
    table.add_column("Annualized APR", justify="right")
    table.add_column("1y Percentile", justify="right")
    table.add_column("Overlay Scale", justify="right")
    for sym in symbols:
        r = fund_rates.get(sym)
        pct = fund_pcts.get(sym)
        sc = ov.scale_for(sym)
        r_str = f"{r * 100:+.4f}%" if r is not None else "—"
        apr_str = f"{r * 3 * 365 * 100:+.2f}%" if r is not None else "—"
        pct_str = f"p{pct:.0f}" if pct is not None else "no history (<90 obs)"
        sc_col = "yellow" if sc < 1.0 else "green"
        table.add_row(sym, r_str, apr_str, pct_str, f"[{sc_col}]{sc:.2f}x[/{sc_col}]")
    console.print(table)

    gate_str = (
        "[yellow]BLOCK NEW ENTRIES (24h macro window)[/yellow]"
        if ov.block_new_entries
        else "[green]OPEN[/green]"
    )
    reasons_str = "; ".join(ov.reasons) if ov.reasons else "none (full 1.00x dial)"
    console.print(
        f"  entry gate: {gate_str} · global scale: [bold]{ov.risk_scale:.2f}x[/bold] · "
        f"reasons: {reasons_str}"
    )
    console.print(f"[dim]feature hash: {feat_hash[:16]}… (~/.vito/features.jsonl)[/dim]")
    return 0


def cmd_shadow(args: argparse.Namespace) -> int:
    """Evaluate live account equity + Simple Earn yield + panel signal and log to audit."""
    policy = load_policy(args.policy)
    gateway = build_gateway(load_credentials())
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]

    if gateway.has_credentials:
        try:
            snap = build_snapshot(gateway)
            equity_usdt = snap.total_usdt
            cash_usdt = snap.cash_usdt
        except ExchangeUnavailable as exc:
            console.print(f"[red]Account snapshot failed:[/red] {exc}")
            return 2
    else:
        equity_usdt = float(args.equity)
        cash_usdt = float(args.equity)

    frames = {}
    with console.status(
        f"evaluating {len(symbols)} instruments ({args.timeframe}) against "
        f"{_money(equity_usdt)} USDT…",
        spinner="dots",
    ):
        for sym in symbols:
            try:
                raw = fetch_recent(gateway, sym, timeframe=args.timeframe, limit=args.bars)
                closed = closed_only(raw, args.timeframe)
                if not closed.empty:
                    frames[sym] = closed
            except ExchangeUnavailable:
                continue

    plan = build_shadow_plan(
        policy=policy,
        gateway=gateway,
        frames=frames,
        equity_usdt=equity_usdt,
        cash_usdt=cash_usdt,
        lookback=args.lookback,
        max_positions=args.max_positions,
        max_exposure=args.max_exposure,
        risk_per_trade_pct=args.risk_pct,
    )

    if args.json:
        console.print_json(json.dumps(asdict(plan), default=str))
        return 0

    console.print(_header(policy))
    console.print(
        f"[bold]Shadow Execution Plan[/bold] · "
        f"live equity [bold]{_money(plan.equity_usdt)}[/bold] USDT · "
        f"risk {args.risk_pct:.1f}% / {args.max_exposure:.2f}x cap · "
        f"Simple Earn [green]{plan.earn_apr_pct:.2f}% APR[/green] "
        f"(+{plan.daily_earn_usdt:.4f} USDT/day)"
    )

    if plan.below_floor:
        console.print(
            f"[red]HALTED: equity {_money(plan.equity_usdt)} USDT is below "
            f"policy floor {_money(policy.equity_floor_usdt)} USDT[/red]"
        )
    elif plan.halted:
        console.print("[red]HALTED: kill switch is engaged[/red]")
    elif not plan.orders:
        console.print(
            f"  [cyan]regime: CASH (no asset in uptrend) → keep 100% "
            f"({_money(plan.idle_cash_usdt)} USDT) in Simple Earn Flexible "
            f"@ {plan.earn_apr_pct:.2f}% APR[/cyan]"
        )
        radar = Table(
            title="Trigger Radar (Daily Close Needed to Enter Tomorrow's Open)",
            title_justify="left",
            header_style="bold",
        )
        rcols = (
            "Instrument",
            "Last Close",
            "20d SMA",
            "20d Ago Close",
            "Trigger Price",
            "Distance",
        )
        for col in rcols:
            radar.add_column(col, justify="left" if col == "Instrument" else "right")
        rows_radar: list[tuple[float, str, float, float, float, float]] = []
        for sym, df in frames.items():
            c = df["close"].astype(float).dropna()
            if len(c) <= args.lookback:
                continue
            last_c = float(c.iloc[-1])
            sma_c = float(c.rolling(args.lookback).mean().iloc[-1])
            ago_c = float(c.iloc[-(args.lookback + 1)])
            trig = max(sma_c, ago_c)
            dist = (trig / last_c - 1.0) * 100.0 if last_c > 0 else 0.0
            rows_radar.append((dist, sym, last_c, sma_c, ago_c, trig))
        for dist, sym, last_c, sma_c, ago_c, trig in sorted(rows_radar):
            radar.add_row(
                sym,
                f"{last_c:,.2f}",
                f"{sma_c:,.2f}",
                f"{ago_c:,.2f}",
                f"{trig:,.2f}",
                f"+{dist:.2f}%",
            )
        console.print(radar)
    else:
        table = Table(title="Intended Orders", title_justify="left", header_style="bold")
        cols = (
            "Instrument",
            "Wallet",
            "Score",
            "Price",
            "Stop",
            "Target USDT",
            "Min USDT",
            "Status",
        )
        for col in cols:
            align = "left" if col in ("Instrument", "Wallet", "Status") else "right"
            table.add_column(col, justify=align)
        for o in plan.orders:
            status_str = "[green]READY[/green]" if o.executable else f"[yellow]{o.reason}[/yellow]"
            table.add_row(
                o.inst_id,
                o.wallet,
                f"{o.score:+.2f}",
                f"{o.ref_price:,.2f}",
                f"{o.stop_price:,.2f} (-{o.stop_distance_pct:.1f}%)",
                _money(o.target_notional_usdt),
                _money(o.min_notional_usdt),
                status_str,
            )
        console.print(table)
        console.print(
            f"  deployed {_money(plan.deployed_usdt)} USDT · "
            f"idle in Simple Earn {_money(plan.idle_cash_usdt)} USDT "
            f"(+{plan.daily_earn_usdt:.4f} USDT/day)"
        )

    console.print(f"[dim]audit hash: {plan.audit_hash[:16]}… ({policy.execution.audit_log})[/dim]")
    return 0


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vito",
        description="Personal quantitative treasury for Binance (and OKX). Read-only by default.",
    )
    parser.add_argument("--version", action="version", version=f"vito {__version__}")
    parser.add_argument(
        "--policy",
        default=None,
        help="path to policy.toml (default: <repo>/config/policy.toml)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="market context and account snapshot")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    policy = sub.add_parser("policy", help="print the risk policy and its digest")
    policy.add_argument("--json", action="store_true", help="machine-readable output")
    policy.set_defaults(func=cmd_policy)

    doctor = sub.add_parser("doctor", help="check environment, connectivity, credentials")
    doctor.set_defaults(func=cmd_doctor)

    setup = sub.add_parser(
        "setup", help="prompt for exchange credentials (hidden input) and write mode-0600 .env"
    )
    setup.add_argument("--venue", default="binance", choices=("binance", "okx"))
    setup.add_argument("--demo", action="store_true", help="enable demo/sandbox mode")
    setup.set_defaults(func=cmd_setup)

    net = sub.add_parser("net", help="show egress IP and whether it matches the allowlist")
    net.add_argument("--json", action="store_true", help="machine-readable output")
    net.set_defaults(func=cmd_net)

    inst = sub.add_parser(
        "instruments",
        help="scan exchange markets for contract specs, liquidity, and small-account tradeability",
    )
    inst.add_argument("--kind", default="all", choices=["all", "swap", "spot"])
    inst.add_argument(
        "--asset-class", default="all", choices=["all", "crypto", "equity", "commodity"]
    )
    inst.add_argument("--equity", type=float, default=200.0, help="account equity in USDT")
    inst.add_argument(
        "--max-exposure",
        type=float,
        default=0.75,
        help="max notional per order as multiple of equity",
    )
    inst.add_argument(
        "--min-vol", type=float, default=100_000.0, help="min 24h volume in USDT"
    )
    inst.add_argument(
        "--max-spread-bps", type=float, default=10.0, help="max bid-ask spread in bps"
    )
    inst.add_argument(
        "--all",
        action="store_true",
        help="include instruments rejected by the liquidity/sizing gate",
    )
    inst.add_argument("--limit", type=int, default=50, help="max rows to display (0 = all)")
    inst.add_argument("--json", action="store_true", help="machine-readable output")
    inst.set_defaults(func=cmd_instruments)

    bt = sub.add_parser("backtest", help="run a strategy over historical candles")
    bt.add_argument("--symbol", default="BTC-USDT-SWAP")
    bt.add_argument("--timeframe", default="4h", choices=sorted(TIMEFRAME_MS))
    bt.add_argument("--days", type=int, default=730)
    bt.add_argument("--strategy", default="all", choices=[*STRATEGIES, "all"])
    bt.add_argument("--initial", type=float, default=200.0, help="starting equity in USDT")
    bt.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side")
    bt.add_argument("--refresh", action="store_true", help="ignore cached candles")
    bt.add_argument("--json", action="store_true", help="machine-readable output")
    bt.set_defaults(func=cmd_backtest)

    wf = sub.add_parser(
        "walkforward", help="choose parameters on the past, score them on the future"
    )
    wf.add_argument("--symbol", default="BTC-USDT-SWAP")
    wf.add_argument("--timeframe", default="1d", choices=sorted(TIMEFRAME_MS))
    wf.add_argument("--days", type=int, default=730)
    wf.add_argument("--strategy", default="breakout", choices=[*STRATEGIES])
    wf.add_argument("--train-bars", type=int, default=365)
    wf.add_argument("--test-bars", type=int, default=90)
    wf.add_argument("--min-train-trades", type=int, default=15)
    wf.add_argument(
        "--min-train-score",
        type=float,
        default=0.0,
        help="do not trade a fold unless the train window beats this score",
    )
    wf.add_argument(
        "--objective", default="sharpe", choices=["sharpe", "expectancy", "return_dd"]
    )
    wf.add_argument("--initial", type=float, default=200.0, help="starting equity in USDT")
    wf.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side")
    wf.add_argument("--refresh", action="store_true", help="ignore cached candles")
    wf.add_argument("--json", action="store_true", help="machine-readable output")
    wf.set_defaults(func=cmd_walkforward)

    panel = sub.add_parser(
        "panel",
        help="run pre-registered cross-asset momentum across a multi-asset basket",
    )
    panel.add_argument(
        "--symbols",
        default=",".join(DEFAULT_BASKET),
        help="comma-separated instrument IDs",
    )
    panel.add_argument("--timeframe", default="1d", choices=sorted(TIMEFRAME_MS))
    panel.add_argument("--days", type=int, default=730)
    panel.add_argument("--lookback", type=int, default=20)
    panel.add_argument("--rebalance-every", type=int, default=5)
    panel.add_argument("--max-positions", type=int, default=2)
    panel.add_argument(
        "--max-per-group",
        type=int,
        default=None,
        help="max positions per correlation family (e.g. 1 prevents double-crypto or double-metal)",
    )
    panel.add_argument("--risk-pct", type=float, default=3.0)
    panel.add_argument("--max-exposure", type=float, default=1.50)
    panel.add_argument("--initial", type=float, default=200.0)
    panel.add_argument("--fee-bps", type=float, default=5.0)
    panel.add_argument(
        "--cash-apr",
        type=float,
        default=8.0,
        help="Simple Earn Flexible APR %% on unallocated USDT cash (default: 8.0)",
    )
    panel.add_argument(
        "--overlay",
        action="store_true",
        help="apply Tier-1 risk-reducing overlay (Fear & Greed, funding, FOMC/CPI calendar)",
    )
    panel.add_argument("--refresh", action="store_true")
    panel.add_argument("--json", action="store_true")
    panel.set_defaults(func=cmd_panel)

    signals = sub.add_parser(
        "signals",
        help="inspect live Tier-1 risk signals (Fear & Greed, funding crowding, FOMC/CPI calendar)",
    )
    signals.add_argument("--symbols", default=",".join(DEFAULT_BASKET))
    signals.add_argument("--refresh", action="store_true")
    signals.add_argument("--json", action="store_true")
    signals.set_defaults(func=cmd_signals)

    shadow = sub.add_parser(
        "shadow",
        help="evaluate live equity, Simple Earn yield, and intended orders into the audit chain",
    )
    shadow.add_argument("--symbols", default=",".join(DEFAULT_BASKET))
    shadow.add_argument("--timeframe", default="1d", choices=sorted(TIMEFRAME_MS))
    shadow.add_argument("--lookback", type=int, default=20)
    shadow.add_argument("--max-positions", type=int, default=2)
    shadow.add_argument("--risk-pct", type=float, default=3.0)
    shadow.add_argument("--max-exposure", type=float, default=1.50)
    shadow.add_argument("--bars", type=int, default=100)
    shadow.add_argument("--equity", type=float, default=200.0)
    shadow.add_argument("--json", action="store_true")
    shadow.set_defaults(func=cmd_shadow)

    paper = sub.add_parser("paper", help="paper trading against live prices")
    paper_sub = paper.add_subparsers(dest="paper_command", required=True)

    step = paper_sub.add_parser("step", help="apply any newly closed bars")
    step.add_argument("--symbol", default="BTC-USDT-SWAP")
    step.add_argument("--timeframe", default="1d", choices=sorted(TIMEFRAME_MS))
    step.add_argument("--strategy", default="breakout", choices=[*STRATEGIES])
    step.add_argument("--param", action="append", help="strategy parameter, key=value")
    step.add_argument("--initial", type=float, default=200.0, help="starting equity in USDT")
    step.add_argument("--fee-bps", type=float, default=5.0, help="taker fee per side")
    step.add_argument("--bars", type=int, default=500, help="candles fetched for warm-up")
    step.add_argument("--state", default=None, help="state file (default: ~/.vito/paper-*.json)")
    step.add_argument("--reset", action="store_true", help="delete the run before stepping")
    step.add_argument("--json", action="store_true", help="machine-readable output")
    step.set_defaults(func=cmd_paper_step)

    show = paper_sub.add_parser("status", help="show paper runs")
    show.add_argument("--state", default=None, help="state file to inspect")
    show.add_argument("--json", action="store_true", help="machine-readable output")
    show.set_defaults(func=cmd_paper_status)

    p_panel = paper_sub.add_parser(
        "panel", help="step cross-asset momentum panel against live closed bars"
    )
    p_panel.add_argument("--symbols", default=",".join(DEFAULT_BASKET))
    p_panel.add_argument("--timeframe", default="1d", choices=sorted(TIMEFRAME_MS))
    p_panel.add_argument("--lookback", type=int, default=20)
    p_panel.add_argument("--rebalance-every", type=int, default=5)
    p_panel.add_argument("--max-positions", type=int, default=2)
    p_panel.add_argument("--risk-pct", type=float, default=3.0)
    p_panel.add_argument("--max-exposure", type=float, default=1.50)
    p_panel.add_argument("--initial", type=float, default=200.0)
    p_panel.add_argument("--fee-bps", type=float, default=5.0)
    p_panel.add_argument("--bars", type=int, default=100)
    p_panel.add_argument("--state", default=None)
    p_panel.add_argument("--reset", action="store_true")
    p_panel.add_argument("--json", action="store_true")
    p_panel.set_defaults(func=cmd_paper_panel)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except PolicyError as exc:
        console.print(f"[red]policy error:[/red] {exc}")
        return 1
    except (MissingCredentials, ExchangeUnavailable) as exc:
        console.print(f"[red]exchange error:[/red] {exc}")
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        console.print("\n[dim]interrupted[/dim]")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
