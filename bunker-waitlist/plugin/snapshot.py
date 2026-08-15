from __future__ import annotations

from dataclasses import dataclass

from plugin.client import ApiRejected, SafeRequestError, fetch_site
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

    if not is_configured():
        return unpublished_monitor(site_ok=site_ok)

    # 1.2.0 will read OpenSea against locked identity. Until then stay unpublished.
    return unpublished_monitor(site_ok=site_ok)
