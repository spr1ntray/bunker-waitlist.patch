from __future__ import annotations

from typing import Any

import httpx

from plugin.proxy import proxy_to_url


def fetch_signed_mint(
    *,
    slug: str,
    contract: str,
    wallet: str,
    proxy: str,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    """Fetch OpenSea allowlist mint payload for one wallet.

    Exact URL/body is filled from a live HAR. Until then this is a closed miss:
    the hunter keeps waiting instead of sending a blind public mint.
    """
    del slug, contract, wallet, timeout_seconds
    try:
        proxy_to_url(proxy)
    except ValueError:
        return None
    return None


def _client(proxy: str, timeout_seconds: int) -> httpx.Client:
    return httpx.Client(
        proxy=proxy_to_url(proxy),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        headers={"Accept": "application/json"},
    )
