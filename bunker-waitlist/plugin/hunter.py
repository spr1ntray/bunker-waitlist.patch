from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from plugin.stage import STAGE_ALLOWLIST, STAGE_ENDED, STAGE_PUBLIC


class HuntTimeout(Exception):
    def __init__(self) -> None:
        super().__init__("hunt_timeout")


def hunt_allowlist(
    *,
    reader: Callable[[], dict[str, Any]],
    deadline_s: float,
    poll_s: float,
    check_cancelled: Callable[[], None],
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll until allowlist is ready. Refuse public. Timeout is not a mint."""
    poll = max(0.2, float(poll_s))
    end = monotonic() + max(1.0, float(deadline_s))
    while True:
        check_cancelled()
        snapshot = reader()
        stage = str(snapshot.get("stage") or "")
        if stage == STAGE_ALLOWLIST and snapshot.get("ready") is True:
            return snapshot
        if stage == STAGE_PUBLIC:
            raise ValueError("public_stage")
        if stage == STAGE_ENDED:
            raise ValueError("stage_ended")
        remaining = end - monotonic()
        if remaining <= 0:
            raise HuntTimeout()
        sleep(min(poll, remaining))
