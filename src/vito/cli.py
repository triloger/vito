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
import json
import platform
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from vito import __version__
from vito.backtest import buy_and_hold, run_backtest
from vito.backtest.walkforward import fold_table, oos_buy_and_hold, walk_forward
from vito.config import load_credentials, load_settings, repo_root
from vito.data.market import MarketRow, market_rows
from vito.data.ohlcv import (
    TIMEFRAME_MS,
    closed_only,
    fetch_candles,
    fetch_funding,
    fetch_recent,
)
from vito.exchange.okx import ExchangeUnavailable, MissingCredentials, OkxGateway
from vito.network import EgressStatus, check_egress, fetch_egress_ip
from vito.paper import PaperBroker, load_state
from vito.policy import Policy, PolicyError, load_policy
from vito.portfolio.snapshot import Snapshot, build_snapshot
from vito.risk import kill_switch_engaged
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
    gateway = OkxGateway(credentials)
    halted = kill_switch_engaged(policy.execution.kill_switch_file)

    try:
        rows = market_rows(gateway, settings.symbols)
    except ExchangeUnavailable as exc:
        console.print(f"[red]OKX unreachable:[/red] {exc}")
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
                "  1. create a [bold]sub-account[/bold] on OKX with only your risk capital\n"
                "  2. API key with [bold]Read[/bold] only, withdraw disabled,\n"
                "     IP allowlist enabled\n"
                "  3. copy [cyan].env.example[/cyan] to [cyan].env[/cyan], fill in the values\n"
                "  4. re-run [bold]vito status[/bold]\n\n"
                "Full walkthrough: [cyan]docs/okx-setup.md[/cyan]",
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

    gateway = OkxGateway(credentials)
    reachable = gateway.ping()
    checks.append(_check("okx reachable", reachable, "api/v5" if reachable else "no response"))
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


# ---------------------------------------------------------------------------
# backtest
# ---------------------------------------------------------------------------


def cmd_backtest(args: argparse.Namespace) -> int:
    policy = load_policy(args.policy)
    gateway = OkxGateway(load_credentials())

    with console.status(f"fetching {args.symbol} {args.timeframe}…", spinner="dots"):
        candles = fetch_candles(
            gateway, args.symbol, timeframe=args.timeframe, days=args.days, refresh=args.refresh
        )
        funding = None
        if args.symbol.endswith("-SWAP"):
            funding = fetch_funding(
                gateway, args.symbol, days=args.days, refresh=args.refresh
            )

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
    gateway = OkxGateway(load_credentials())

    with console.status(f"fetching {args.symbol} {args.timeframe}…", spinner="dots"):
        candles = fetch_candles(
            gateway, args.symbol, timeframe=args.timeframe, days=args.days, refresh=args.refresh
        )
        funding = None
        if args.symbol.endswith("-SWAP"):
            funding = fetch_funding(gateway, args.symbol, days=args.days, refresh=args.refresh)

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
        f"max DD [red]{m['max_drawdown_pct']:.1f}%[/red] · sharpe {m['sharpe']:.2f} · "
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
    gateway = OkxGateway(load_credentials())

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
# entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vito",
        description="Personal quantitative treasury for OKX. Read-only by default.",
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

    net = sub.add_parser("net", help="show egress IP and whether it matches the allowlist")
    net.add_argument("--json", action="store_true", help="machine-readable output")
    net.set_defaults(func=cmd_net)

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
    wf.add_argument("--min-train-trades", type=int, default=4)
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
