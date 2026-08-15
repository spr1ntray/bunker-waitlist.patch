"""Official BUNKER collection identity. Empty until 1.2.0.

Fill these only in a new SemVer. Do not edit an installed package.
"""

from __future__ import annotations

COLLECTION_SLUG: str | None = None
CHAIN_ID: int | None = None
CONTRACT: str | None = None


def is_configured() -> bool:
    slug = (COLLECTION_SLUG or "").strip()
    contract = (CONTRACT or "").strip()
    return bool(slug and contract and isinstance(CHAIN_ID, int) and CHAIN_ID > 0)
