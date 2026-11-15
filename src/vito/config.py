"""Environment and filesystem configuration.

Secrets come from the environment only (optionally via a local ``.env``).
Nothing secret is ever written to a committed file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # pragma: no cover - optional dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]


def repo_root() -> Path:
    """Locate the project root.

    Order: ``$VITO_HOME``, then walk up from the current directory looking for
    ``config/policy.toml``, then the current directory.
    """
    env_home = os.environ.get("VITO_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    here = Path.cwd().resolve()
    for candidate in (here, *here.parents):
        if (candidate / "config" / "policy.toml").exists():
            return candidate
    return here


def load_env(root: Path | None = None) -> None:
    """Load ``<root>/.env`` into the process environment if present."""
    root = root or repo_root()
    env_file = root / ".env"
    if load_dotenv is not None and env_file.exists():
        load_dotenv(env_file, override=False)


@dataclass(frozen=True, slots=True)
class Credentials:
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""
    demo: bool = False

    @property
    def complete(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def masked(self) -> str:
        if not self.api_key:
            return "absent"
        return f"{self.api_key[:4]}…{self.api_key[-4:]}"


def load_credentials() -> Credentials:
    load_env()
    return Credentials(
        api_key=os.environ.get("VITO_OKX_API_KEY", "").strip(),
        api_secret=os.environ.get("VITO_OKX_API_SECRET", "").strip(),
        passphrase=os.environ.get("VITO_OKX_API_PASSPHRASE", "").strip(),
        demo=os.environ.get("VITO_OKX_DEMO", "0") == "1",
    )


@dataclass(frozen=True, slots=True)
class Settings:
    symbols: tuple[str, ...]
    spot_symbols: tuple[str, ...]
    expected_egress_ip: str = ""
    min_width: int = 80


DEFAULT_SYMBOLS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP")
DEFAULT_SPOT = ("BTC-USDT", "ETH-USDT")


def load_settings(root: Path | None = None) -> Settings:
    """Load ``config/config.toml`` if present, else fall back to defaults."""
    import tomllib

    root = root or repo_root()
    path = root / "config" / "config.toml"
    if not path.exists():
        return Settings(symbols=DEFAULT_SYMBOLS, spot_symbols=DEFAULT_SPOT)

    raw = tomllib.loads(path.read_text())
    market = raw.get("market", {})
    ui = raw.get("ui", {})
    network = raw.get("network", {})
    return Settings(
        symbols=tuple(market.get("symbols", DEFAULT_SYMBOLS)),
        spot_symbols=tuple(market.get("spot_symbols", DEFAULT_SPOT)),
        expected_egress_ip=str(network.get("expected_egress_ip", "") or ""),
        min_width=int(ui.get("min_width", 80)),
    )
