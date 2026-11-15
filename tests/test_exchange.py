from __future__ import annotations

import pytest

from vito.config import Credentials
from vito.exchange.okx import (
    MissingCredentials,
    OkxGateway,
    _interval_hours,
)


@pytest.mark.parametrize(
    ("interval", "hours"),
    [(None, 8.0), ("8h", 8.0), ("4h", 4.0), ("1h", 1.0), ("240m", 4.0), ("", 8.0)],
)
def test_funding_interval_parsing(interval, hours) -> None:
    assert _interval_hours(interval) == hours


def test_public_client_has_no_credentials() -> None:
    gateway = OkxGateway(Credentials())
    assert gateway.has_credentials is False


def test_private_call_raises_without_credentials() -> None:
    gateway = OkxGateway(Credentials())
    with pytest.raises(MissingCredentials):
        gateway.balances()


def test_partial_credentials_are_not_treated_as_complete() -> None:
    gateway = OkxGateway(Credentials(api_key="k", api_secret="s"))
    assert gateway.has_credentials is False
    with pytest.raises(MissingCredentials):
        gateway.balances()


def test_credentials_are_masked() -> None:
    assert Credentials().masked() == "absent"
    assert Credentials(api_key="abcd1234efgh").masked() == "abcd…efgh"
