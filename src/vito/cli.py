"""Command line interface.

Commands
--------
``vito status``   market context + account snapshot (read-only)
``vito policy``   the risk constitution and its digest
``vito doctor``   environment, connectivity, and credential checks

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
from vito.config import load_credentials, load_settings, repo_root
from vito.data.market import MarketRow, market_rows
from vito.exchange.okx import ExchangeUnavailable, MissingCredentials, OkxGateway
from vito.network import EgressStatus, check_egress, fetch_egress_ip
from vito.policy import Policy, PolicyError, load_policy
from vito.portfolio.snapshot import Snapshot, build_snapshot
from vito.risk import kill_switch_engaged

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
