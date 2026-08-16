from __future__ import annotations

from collections.abc import Callable
from typing import Any

from plugin.discover import DiscoveredCollection, discover_collection
from plugin.hunter import hunt_allowlist
from plugin.identity import locked_contract, locked_slug
from plugin.opensea_drop import DropRejected, read_drop_snapshot
from plugin.opensea_sign import fetch_signed_mint
from plugin.stage import STAGE_ALLOWLIST, STAGE_WAIT, classify_stage


def make_stage_reader(
    *,
    proxy: str,
    timeout_seconds: int,
    wallet: str,
    discover_fn: Callable[..., DiscoveredCollection | None] = discover_collection,
    windows_fn: Callable[..., dict[str, int]] | None = None,
) -> Callable[[], dict[str, Any]]:
    state: dict[str, str | None] = {
        "contract": locked_contract(),
        "slug": locked_slug(),
    }

    def reader() -> dict[str, Any]:
        if not state["contract"]:
            found = discover_fn(proxy=proxy, timeout_seconds=timeout_seconds)
            if found is not None:
                state["contract"] = found.contract
                state["slug"] = found.slug
        if not state["contract"] or not state["slug"]:
            return {
                "stage": STAGE_WAIT,
                "ready": False,
                "reason": "unpublished",
                "contract": "",
                "slug": "",
            }
        windows = (windows_fn or _empty_windows)(
            contract=str(state["contract"]),
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        stage = classify_stage(
            now=int(windows.get("now") or 0),
            allow_start=int(windows.get("allow_start") or 0),
            allow_end=int(windows.get("allow_end") or 0),
            public_start=int(windows.get("public_start") or 0),
            public_end=int(windows.get("public_end") or 0),
        )
        return {
            "stage": stage,
            "ready": stage == STAGE_ALLOWLIST,
            "reason": stage,
            "contract": state["contract"],
            "slug": state["slug"],
        }

    return reader


def wait_for_allowlist(
    *,
    proxy: str,
    timeout_seconds: int,
    wallet: str,
    deadline_s: float,
    poll_s: float,
    check_cancelled: Callable[[], None],
    on_wait: Callable[[dict[str, Any]], None] | None = None,
    **reader_hooks: Any,
) -> dict[str, Any]:
    if reader_hooks:
        reader = make_stage_reader(
            proxy=proxy,
            timeout_seconds=timeout_seconds,
            wallet=wallet,
            **reader_hooks,
        )
    else:

        def reader() -> dict[str, Any]:
            try:
                return read_drop_snapshot(proxy=proxy, timeout_seconds=timeout_seconds)
            except DropRejected:
                return {
                    "stage": STAGE_WAIT,
                    "ready": False,
                    "reason": "unpublished",
                    "contract": "",
                    "slug": "",
                }

    return hunt_allowlist(
        reader=reader,
        deadline_s=deadline_s,
        poll_s=poll_s,
        check_cancelled=check_cancelled,
        on_wait=on_wait,
    )


def wait_for_signed_payload(
    *,
    slug: str,
    contract: str,
    wallet: str,
    proxy: str,
    timeout_seconds: int,
    deadline_s: float,
    poll_s: float,
    check_cancelled: Callable[[], None],
    sign_fn: Callable[..., dict[str, Any] | None] = fetch_signed_mint,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any] | None:
    import time as _time

    clock = monotonic or _time.monotonic
    nap = sleep or _time.sleep
    poll = max(0.2, float(poll_s))
    end = clock() + max(1.0, float(deadline_s))
    while True:
        check_cancelled()
        signed = sign_fn(
            slug=slug,
            contract=contract,
            wallet=wallet,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        if signed is not None:
            return signed
        remaining = end - clock()
        if remaining <= 0:
            return None
        nap(min(poll, remaining))


def _empty_windows(**_kwargs: Any) -> dict[str, int]:
    return {
        "now": 0,
        "allow_start": 0,
        "allow_end": 0,
        "public_start": 0,
        "public_end": 0,
    }
