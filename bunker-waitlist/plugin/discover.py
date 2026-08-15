from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from plugin.identity import OPENSEA_CHAIN, slug_search_list
from plugin.proxy import proxy_to_url


@dataclass(frozen=True)
class DiscoveredCollection:
    slug: str
    contract: str


def discover_collection(*, proxy: str, timeout_seconds: int) -> DiscoveredCollection | None:
    """Best-effort OpenSea lookup by locked slug list. Never guesses eligible."""
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/142.0.0.0 Safari/537.36"
        ),
    }
    try:
        with httpx.Client(
            proxy=proxy_to_url(proxy),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers=headers,
        ) as client:
            for slug in slug_search_list():
                found = _read_slug(client, slug)
                if found is not None:
                    return found
    except httpx.HTTPError:
        return None
    return None


def _read_slug(client: httpx.Client, slug: str) -> DiscoveredCollection | None:
    url = f"https://api.opensea.io/api/v2/collections/{slug}"
    try:
        response = client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    contract = _contract_from_collection(data)
    if not contract:
        return None
    return DiscoveredCollection(slug=slug, contract=contract)


def _contract_from_collection(data: dict[str, Any]) -> str | None:
    contracts = data.get("contracts")
    if isinstance(contracts, list):
        for item in contracts:
            if not isinstance(item, dict):
                continue
            chain = str(item.get("chain") or "").lower()
            address = str(item.get("address") or "").strip()
            if address.startswith("0x") and len(address) == 42:
                if chain in {"", OPENSEA_CHAIN, "robinhood_chain", "hood"}:
                    return address.lower()
        for item in contracts:
            if not isinstance(item, dict):
                continue
            address = str(item.get("address") or "").strip()
            if address.startswith("0x") and len(address) == 42:
                return address.lower()
    return None
