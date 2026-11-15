"""Order routing — disabled by default.

Live routing lands in Phase 3. What exists now is the lock, because the lock
is the part worth writing first: arming requires the operator to type the
current policy digest, and the digest changes whenever any risk parameter
changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from vito.policy import Policy


class ExecutionLocked(RuntimeError):
    """Raised when an order is attempted while the router is not armed."""


@dataclass
class OrderRouter:
    policy: Policy
    _armed: bool = False

    def arm(self, confirmation: str) -> None:
        """Arm live routing. ``confirmation`` must equal the policy digest."""
        if self.policy.execution.mode == "readonly":
            raise ExecutionLocked(
                "policy mode is 'readonly'; set execution.mode to 'live' first"
            )
        if os.environ.get("VITO_LIVE") != "1":
            raise ExecutionLocked("VITO_LIVE=1 is required to arm live routing")
        if confirmation != self.policy.digest():
            raise ExecutionLocked(
                f"digest mismatch: policy is {self.policy.digest()}, "
                "re-read `vito policy` and try again"
            )
        if os.path.exists(os.path.expanduser(self.policy.execution.kill_switch_file)):
            raise ExecutionLocked("kill switch is engaged; remove the HALT file first")
        self._armed = True

    @property
    def armed(self) -> bool:
        return self._armed

    def place(self, order: dict) -> None:
        if not self._armed:
            raise ExecutionLocked(
                "router is not armed; no order can leave this process"
            )
        raise NotImplementedError(
            "Live order routing lands in Phase 3, after backtest and paper "
            "trading produce a positive sample."
        )
