from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from vito.policy import Policy

ROOT = Path(__file__).resolve().parents[1]

BASE_POLICY: dict[str, Any] = {
    "version": "test",
    "equity_floor_usdt": 50.0,
    "risk": {
        "risk_per_trade_pct": 1.5,
        "conviction_risk_pct": 2.0,
        "max_leverage": 6.0,
        "max_gross_exposure_mult": 3.0,
        "max_open_positions": 2,
        "min_stop_distance_pct": 0.6,
        "liquidation_buffer_mult": 5.0,
        "max_slippage_pct": 0.15,
        "maintenance_margin_rate": 0.004,
        "funding_guard_minutes": 15,
    },
    "limits": {
        "daily_loss_halt_pct": 3.0,
        "peak_drawdown_stop_pct": 15.0,
        "loss_streak_cooldown": 4,
        "cooldown_hours": 24,
    },
    "instruments": {
        "allowed": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"],
        "conviction_allowed": ["BTC-USDT-SWAP"],
    },
    "execution": {
        "mode": "readonly",
        "require_policy_hash_confirm": True,
        "kill_switch_file": "~/.vito/HALT",
        "audit_log": "~/.vito/audit.jsonl",
        "allow_live_from_config": False,
    },
}


@pytest.fixture()
def policy() -> Policy:
    """The real shipped policy, loaded from config/policy.toml."""
    return Policy.load(ROOT / "config" / "policy.toml")


@pytest.fixture()
def make_policy():
    """Factory for mutated policies in edge-case tests."""

    def _make(**overrides: Any) -> Policy:
        raw = copy.deepcopy(BASE_POLICY)
        for dotted, value in overrides.items():
            section, _, key = dotted.partition("__")
            if key:
                raw[section][key] = value
            else:
                raw[section] = value
        return Policy.from_dict(raw)

    return _make
