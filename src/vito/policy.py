"""Typed, validated view over ``config/policy.toml``.

The policy is immutable. Loading it produces a digest; any edit to the file
changes the digest, which in turn invalidates an armed live executor. Risk
parameters are not allowed to drift silently.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

EXECUTION_MODES = ("readonly", "paper", "live")


class PolicyError(ValueError):
    """Raised when the policy file is missing, malformed, or unsafe."""


@dataclass(frozen=True, slots=True)
class RiskPolicy:
    """Per-trade risk controls. Percentages are expressed in *percent* units."""

    risk_per_trade_pct: float
    conviction_risk_pct: float
    max_leverage: float
    max_gross_exposure_mult: float
    max_open_positions: int
    min_stop_distance_pct: float
    liquidation_buffer_mult: float
    max_slippage_pct: float
    maintenance_margin_rate: float
    funding_guard_minutes: int


@dataclass(frozen=True, slots=True)
class Limits:
    """Account-level circuit breakers."""

    daily_loss_halt_pct: float
    peak_drawdown_stop_pct: float
    loss_streak_cooldown: int
    cooldown_hours: int


@dataclass(frozen=True, slots=True)
class Instruments:
    """Whitelist. Anything not listed cannot be traded, at any size."""

    allowed: tuple[str, ...]
    conviction_allowed: tuple[str, ...]

    def is_allowed(self, inst_id: str) -> bool:
        return inst_id in self.allowed

    def allows_conviction(self, inst_id: str) -> bool:
        return inst_id in self.conviction_allowed


@dataclass(frozen=True, slots=True)
class Execution:
    mode: str
    require_policy_hash_confirm: bool
    kill_switch_file: str
    audit_log: str
    allow_live_from_config: bool = False


@dataclass(frozen=True, slots=True)
class Policy:
    version: str
    equity_floor_usdt: float
    risk: RiskPolicy
    limits: Limits
    instruments: Instruments
    execution: Execution

    # -- serialisation -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def canonical(self) -> str:
        """Stable, order-independent JSON used for hashing."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        """Short content hash. Shown to the operator before arming live mode."""
        return hashlib.sha256(self.canonical().encode()).hexdigest()[:16]

    # -- helpers -----------------------------------------------------------

    def risk_fraction(self, conviction: bool = False, instrument: str = "") -> float:
        """Risk budget per trade as a fraction of equity (0.015 == 1.5%)."""
        if conviction and self.instruments.allows_conviction(instrument):
            return self.risk.conviction_risk_pct / 100.0
        return self.risk.risk_per_trade_pct / 100.0

    @property
    def min_stop_fraction(self) -> float:
        return self.risk.min_stop_distance_pct / 100.0

    # -- construction ------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Policy:
        try:
            policy = cls(
                version=str(raw["version"]),
                equity_floor_usdt=float(raw["equity_floor_usdt"]),
                risk=RiskPolicy(**_section(raw, "risk", RiskPolicy)),
                limits=Limits(**_section(raw, "limits", Limits)),
                instruments=Instruments(
                    allowed=tuple(raw["instruments"]["allowed"]),
                    conviction_allowed=tuple(raw["instruments"].get("conviction_allowed", [])),
                ),
                execution=Execution(
                    mode=str(raw["execution"]["mode"]),
                    require_policy_hash_confirm=bool(
                        raw["execution"]["require_policy_hash_confirm"]
                    ),
                    kill_switch_file=str(raw["execution"]["kill_switch_file"]),
                    audit_log=str(raw["execution"]["audit_log"]),
                    allow_live_from_config=bool(
                        raw["execution"].get("allow_live_from_config", False)
                    ),
                ),
            )
        except KeyError as exc:  # pragma: no cover - defensive
            raise PolicyError(f"policy is missing required key: {exc}") from exc
        except TypeError as exc:
            raise PolicyError(f"policy has unexpected fields: {exc}") from exc

        policy.validate()
        return policy

    @classmethod
    def load(cls, path: str | Path) -> Policy:
        path = Path(path)
        if not path.exists():
            raise PolicyError(f"policy file not found: {path}")
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise PolicyError(f"policy file is not valid TOML: {exc}") from exc
        return cls.from_dict(raw)

    # -- invariants --------------------------------------------------------

    def validate(self) -> None:
        r, lim = self.risk, self.limits
        problems: list[str] = []

        if self.equity_floor_usdt <= 0:
            problems.append("equity_floor_usdt must be > 0")
        if not 0 < r.risk_per_trade_pct <= 5:
            problems.append("risk_per_trade_pct must be in (0, 5]")
        if r.conviction_risk_pct < r.risk_per_trade_pct:
            problems.append("conviction_risk_pct must be >= risk_per_trade_pct")
        if not 1 <= r.max_leverage <= 25:
            problems.append("max_leverage must be in [1, 25]")
        if not 1 <= r.max_gross_exposure_mult <= 5:
            problems.append("max_gross_exposure_mult must be in [1, 5]")
        if r.max_open_positions < 1:
            problems.append("max_open_positions must be >= 1")
        if not 0 < r.min_stop_distance_pct < 20:
            problems.append("min_stop_distance_pct must be in (0, 20)")
        if r.liquidation_buffer_mult < 2:
            problems.append("liquidation_buffer_mult must be >= 2")
        if not 0 <= r.maintenance_margin_rate < 0.1:
            problems.append("maintenance_margin_rate must be in [0, 0.1)")
        if lim.daily_loss_halt_pct <= 0:
            problems.append("daily_loss_halt_pct must be > 0")
        if lim.daily_loss_halt_pct > lim.peak_drawdown_stop_pct:
            problems.append("daily_loss_halt_pct must be <= peak_drawdown_stop_pct")
        if lim.loss_streak_cooldown < 1:
            problems.append("loss_streak_cooldown must be >= 1")
        if self.execution.mode not in EXECUTION_MODES:
            problems.append(f"execution.mode must be one of {EXECUTION_MODES}")
        if self.execution.allow_live_from_config:
            problems.append(
                "execution.allow_live_from_config must be false: arming live mode "
                "is a deliberate CLI action, never a config edit"
            )
        if not self.instruments.allowed:
            problems.append("instruments.allowed must not be empty")
        unknown = set(self.instruments.conviction_allowed) - set(self.instruments.allowed)
        if unknown:
            problems.append(f"conviction_allowed not in allowed list: {sorted(unknown)}")

        if problems:
            raise PolicyError("invalid policy:\n  - " + "\n  - ".join(problems))


def _section(raw: dict[str, Any], name: str, kind: type) -> dict[str, Any]:
    section = raw.get(name)
    if not isinstance(section, dict):
        raise PolicyError(f"policy section [{name}] is missing")
    expected = set(kind.__dataclass_fields__)  # type: ignore[attr-defined]
    missing = expected - set(section)
    extra = set(section) - expected
    if missing:
        raise PolicyError(f"policy section [{name}] missing: {sorted(missing)}")
    if extra:
        raise PolicyError(f"policy section [{name}] has unknown keys: {sorted(extra)}")
    return section


def load_policy(path: str | Path | None = None) -> Policy:
    """Load the policy, defaulting to ``<repo>/config/policy.toml``."""
    if path is not None:
        return Policy.load(path)
    from vito.config import repo_root

    return Policy.load(repo_root() / "config" / "policy.toml")
