"""Egress IP detection and allowlist drift checks.

OKX rejects API calls from addresses that are not on the key's allowlist. On a
residential connection the address changes without warning, and the failure mode
is a locked-out key at the worst possible moment. This module makes that drift
visible before the exchange notices it.
"""

from __future__ import annotations

import ipaddress
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

DEFAULT_PROVIDERS = (
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://icanhazip.com",
)


class EgressStatus(StrEnum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNCONFIGURED = "unconfigured"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True, slots=True)
class Egress:
    ip: str | None
    provider: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.ip is not None


def normalize_ip(value: str) -> str | None:
    """Parse an address, tolerating ``ip:port`` and whitespace."""
    value = value.strip()
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    host = value.rsplit(":", 1)[0] if value.count(":") == 1 else value
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def ip_in_allowlist(ip: str, entries: Iterable[str]) -> bool:
    """True when ``ip`` matches any allowlist entry (exact, or CIDR form)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for entry in entries:
        entry = entry.strip()
        if not entry:
            continue
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False


def check_egress(expected: str | None, actual: str | None) -> EgressStatus:
    """Compare the configured allowlist entry with what we actually egress from."""
    if not expected:
        return EgressStatus.UNCONFIGURED
    if not actual:
        return EgressStatus.UNREACHABLE
    return (
        EgressStatus.MATCH
        if ip_in_allowlist(actual, [expected])
        else EgressStatus.MISMATCH
    )


def fetch_egress_ip(
    providers: Iterable[str] = DEFAULT_PROVIDERS, timeout: float = 5.0
) -> Egress:
    """Ask each provider in turn for our public address.

    Several providers are tried because a single one being down should not look
    like a network outage. No provider is trusted for anything beyond the string
    it returns.
    """
    errors: list[str] = []
    for url in providers:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                raw = response.read(64).decode("utf-8", "ignore")
        except Exception as exc:  # noqa: BLE001 - report, don't raise
            errors.append(f"{url}: {type(exc).__name__}")
            continue
        ip = normalize_ip(raw)
        if ip:
            return Egress(ip=ip, provider=url)
        errors.append(f"{url}: unparseable response")
    return Egress(ip=None, error="; ".join(errors) or "no providers configured")
