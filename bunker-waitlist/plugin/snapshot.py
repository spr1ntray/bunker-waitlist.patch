from __future__ import annotations

from dataclasses import dataclass

from plugin.client import ApiRejected, SafeRequestError, fetch_site
from plugin.discover import discover_collection
from plugin.identity import is_configured


@dataclass(frozen=True)
class AccountMonitor:
    outcome: str
    collection_status: str
    eligible: bool
    mint_stage: str
    floor_eth: str
    owned: int
    site_ok: bool

    def as_data(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "collection_status": self.collection_status,
            "eligible": self.eligible,
            "mint_stage": self.mint_stage,
            "floor_eth": self.floor_eth,
            "owned": self.owned,
            "site_ok": self.site_ok,
        }


def unpublished_monitor(*, site_ok: bool) -> AccountMonitor:
    return AccountMonitor(
        outcome="unpublished" if site_ok else "failed",
        collection_status="unpublished",
        eligible=False,
        mint_stage="unpublished",
        floor_eth="",
        owned=0,
        site_ok=site_ok,
    )


def collect_monitor(*, proxy: str, timeout_seconds: int) -> AccountMonitor:
    """Read-only snapshot. Never submits, signs, or calls OpenSea in 1.1.0."""
    try:
        site = fetch_site(proxy=proxy, timeout_seconds=timeout_seconds)
        site_ok = bool(site.ok)
    except (SafeRequestError, ApiRejected):
        return unpublished_monitor(site_ok=False)

    if is_configured():
        return AccountMonitor(
            outcome="ok" if site_ok else "failed",
            collection_status="live",
            eligible=False,
            mint_stage="unknown",
            floor_eth="",
            owned=0,
            site_ok=site_ok,
        )

    found = discover_collection(proxy=proxy, timeout_seconds=timeout_seconds)
    if found is None:
        return unpublished_monitor(site_ok=site_ok)
    return AccountMonitor(
        outcome="ok" if site_ok else "failed",
        collection_status="live",
        eligible=False,
        mint_stage="unknown",
        floor_eth="",
        owned=0,
        site_ok=site_ok,
    )
