"""Fail-closed collection identity. Guessed OpenSea slugs are not enough."""

from __future__ import annotations

import re
from dataclasses import dataclass

from plugin.client import SITE_ORIGIN
from plugin.identity import SEADROP, locked_contract, locked_slug
from plugin.proxy import proxy_to_url

import httpx

_SLUG_RE = re.compile(
    r"opensea\.io/(?:zh-TW/|zh-CN/|ja/)?collection/([a-z0-9][a-z0-9_-]{0,80})",
    re.IGNORECASE,
)
_ADDR_RE = re.compile(r"0x[a-fA-F0-9]{40}")
_ROBINHOOD = {"robinhood", "robinhood_chain", "hood"}
_OFFICIAL_PATHS = ("/", "/enter")


@dataclass(frozen=True)
class OfficialRefs:
    slugs: tuple[str, ...]
    contracts: tuple[str, ...]


def extract_official_refs(text: str) -> OfficialRefs:
    slugs = tuple(dict.fromkeys(match.group(1).lower() for match in _SLUG_RE.finditer(text or "")))
    banned = {SEADROP.lower()}
    contracts = tuple(
        dict.fromkeys(
            match.group(0).lower()
            for match in _ADDR_RE.finditer(text or "")
            if match.group(0).lower() not in banned
        )
    )
    return OfficialRefs(slugs=slugs, contracts=contracts)


def _chain_ok(drop: dict[str, object]) -> bool:
    chain = str(drop.get("chain") or drop.get("chainIdentifier") or "").strip().lower()
    return not chain or chain in _ROBINHOOD


def drop_is_trusted(
    drop: dict[str, object],
    *,
    slugs: tuple[str, ...],
    contracts: tuple[str, ...],
    locked_slug: str | None = None,
    locked_contract: str | None = None,
) -> bool:
    if not _chain_ok(drop):
        return False
    slug = str(drop.get("collection_slug") or drop.get("collectionSlug") or "").strip().lower()
    contract = str(drop.get("contract_address") or drop.get("contractAddress") or "").strip().lower()
    if not slug or not contract.startswith("0x") or len(contract) != 42:
        return False

    want_slug = (locked_slug or "").strip().lower()
    want_contract = (locked_contract or "").strip().lower()
    if want_slug or want_contract:
        if want_slug and slug != want_slug:
            return False
        if want_contract and contract != want_contract:
            return False
        return True

    official_slugs = {item.lower() for item in slugs}
    official_contracts = {item.lower() for item in contracts}
    if official_contracts and contract not in official_contracts:
        return False
    if official_slugs and slug in official_slugs:
        return True
    if official_contracts and contract in official_contracts:
        return True
    return False


def fetch_official_refs(*, proxy: str, timeout_seconds: int) -> OfficialRefs:
    slugs: list[str] = []
    contracts: list[str] = []
    try:
        with httpx.Client(
            proxy=proxy_to_url(proxy),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/142.0.0.0 Safari/537.36"
                )
            },
        ) as client:
            for path in _OFFICIAL_PATHS:
                try:
                    response = client.get(f"{SITE_ORIGIN}{path}")
                except httpx.HTTPError:
                    continue
                if response.status_code != 200:
                    continue
                found = extract_official_refs(response.text or "")
                slugs.extend(found.slugs)
                contracts.extend(found.contracts)
    except httpx.HTTPError:
        return OfficialRefs(slugs=(), contracts=())
    return OfficialRefs(
        slugs=tuple(dict.fromkeys(slugs)),
        contracts=tuple(dict.fromkeys(contracts)),
    )


def trusted_drop(drop: dict[str, object] | None, refs: OfficialRefs) -> dict[str, object] | None:
    if not drop:
        return None
    if drop_is_trusted(
        drop,
        slugs=refs.slugs,
        contracts=refs.contracts,
        locked_slug=locked_slug(),
        locked_contract=locked_contract(),
    ):
        return drop
    return None
