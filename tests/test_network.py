from __future__ import annotations

import pytest

from vito import network
from vito.network import (
    Egress,
    EgressStatus,
    check_egress,
    fetch_egress_ip,
    ip_in_allowlist,
    normalize_ip,
)

# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("152.166.28.231", "152.166.28.231"),
        ("  152.166.28.231\n", "152.166.28.231"),
        ("152.166.28.231:443", "152.166.28.231"),
        ("2606:4700::1111", "2606:4700::1111"),
        ("not-an-ip", None),
        ("", None),
        ("152.166.28.999", None),
    ],
)
def test_normalize_ip(raw, expected) -> None:
    assert normalize_ip(raw) == expected


# ---------------------------------------------------------------------------
# allowlist matching
# ---------------------------------------------------------------------------


def test_exact_match() -> None:
    assert ip_in_allowlist("152.166.28.231", ["152.166.28.231"])


def test_cidr_entry_matches() -> None:
    assert ip_in_allowlist("152.166.28.231", ["152.166.28.0/24"])


def test_no_match() -> None:
    assert not ip_in_allowlist("152.166.28.232", ["152.166.28.231"])


def test_blank_and_garbage_entries_are_skipped() -> None:
    assert ip_in_allowlist("152.166.28.231", ["", "  ", "nonsense", "152.166.28.231"])


def test_family_mismatch_is_false_not_an_error() -> None:
    assert not ip_in_allowlist("2606:4700::1111", ["152.166.28.231"])


def test_malformed_candidate_is_false() -> None:
    assert not ip_in_allowlist("garbage", ["garbage"])


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_unconfigured() -> None:
    assert check_egress("", "152.166.28.231") is EgressStatus.UNCONFIGURED


def test_status_unreachable() -> None:
    assert check_egress("152.166.28.231", None) is EgressStatus.UNREACHABLE


def test_status_match() -> None:
    assert check_egress("152.166.28.231", "152.166.28.231") is EgressStatus.MATCH


def test_status_mismatch() -> None:
    assert check_egress("152.166.28.231", "81.2.69.142") is EgressStatus.MISMATCH


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, _n: int) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def test_fetch_uses_first_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        network.urllib.request, "urlopen", lambda url, timeout: _Response(b"81.2.69.142\n")
    )
    result = fetch_egress_ip(providers=("https://a", "https://b"))
    assert result.ip == "81.2.69.142"
    assert result.provider == "https://a"


def test_fetch_falls_back_after_failure(monkeypatch) -> None:
    responses = {"https://bad": OSError("down"), "https://good": _Response(b"81.2.69.142")}

    def fake(url, timeout):
        value = responses[url]
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(network.urllib.request, "urlopen", fake)
    result = fetch_egress_ip(providers=("https://bad", "https://good"))
    assert result.ip == "81.2.69.142"
    assert result.provider == "https://good"


def test_fetch_reports_failure(monkeypatch) -> None:
    def boom(url, timeout):
        raise OSError("network down")

    monkeypatch.setattr(network.urllib.request, "urlopen", boom)
    result = fetch_egress_ip(providers=("https://a",))
    assert result.ip is None
    assert result.ok is False
    assert "network down" not in (result.error or "")  # only the exception type


def test_egress_ok_flag() -> None:
    assert Egress(ip="1.2.3.4").ok
    assert not Egress(ip=None).ok
