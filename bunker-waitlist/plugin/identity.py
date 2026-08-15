"""Official BUNKER collection identity and shared OpenSea/Robinhood profile.

Fill COLLECTION_SLUG and CONTRACT only in a new SemVer.
Do not edit an installed package.
"""

from __future__ import annotations

CHAIN_ID = 4663
RPC_URL = "https://rpc.mainnet.chain.robinhood.com"
# Canonical SeaDrop used by OpenSea on EVM chains (CREATE2).
SEADROP = "0x00005EA00Ac477B1030CE78506496e8C2dE24bf5"
OPENSEA_CHAIN = "robinhood"
COLLECTION_SLUG: str | None = None
CONTRACT: str | None = None
SLUG_CANDIDATES: tuple[str, ...] = (
    "thebunkerhood",
    "bunkerhood",
    "bunker-hood",
    "the-bunker",
    "bunkerhood-official",
    "enter-the-bunker",
)


def is_configured() -> bool:
    slug = (COLLECTION_SLUG or "").strip()
    contract = (CONTRACT or "").strip()
    return bool(slug and contract and CHAIN_ID > 0)


def locked_contract() -> str | None:
    value = (CONTRACT or "").strip()
    return value.lower() if value else None


def locked_slug() -> str | None:
    value = (COLLECTION_SLUG or "").strip()
    return value if value else None


def slug_search_list() -> tuple[str, ...]:
    locked = locked_slug()
    if locked:
        return (locked,)
    return SLUG_CANDIDATES
