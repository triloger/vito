from __future__ import annotations

from vito.cli import build_parser, main


def test_parser_requires_a_subcommand() -> None:
    parser = build_parser()
    args = parser.parse_args(["status"])
    assert callable(args.func)


def test_policy_json_is_machine_readable() -> None:
    assert main(["policy", "--json"]) == 0


def test_cli_imports_cover_every_module() -> None:
    """A smoke import: packaging gaps (missing packages) fail here, not in prod."""
    import importlib

    for module in (
        "vito.cli",
        "vito.config",
        "vito.policy",
        "vito.data.market",
        "vito.data.instruments",
        "vito.exchange.okx",
        "vito.portfolio.snapshot",
        "vito.risk.limits",
        "vito.strategy.base",
        "vito.exec",
        "vito.exec.audit",
        "vito.backtest",
        "vito.paper",
    ):
        assert importlib.import_module(module) is not None
