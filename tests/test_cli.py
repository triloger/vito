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
        "vito.exchange",
        "vito.exchange.binance",
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


def test_write_env_credentials_sets_0600_mode(tmp_path) -> None:
    import stat

    from vito.config import write_env_credentials

    env_file = write_env_credentials("abcd1234efgh", "sec", "pass", root=tmp_path)
    assert env_file.exists()
    mode = stat.S_IMODE(env_file.stat().st_mode)
    assert mode == 0o600
    text = env_file.read_text(encoding="utf-8")
    assert "VITO_OKX_API_KEY=abcd1234efgh" in text
    assert "VITO_OKX_DEMO=0" in text
