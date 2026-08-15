from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass

from plugin.handles import HANDLES
from plugin.posts import POSTS

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
COMMENTS = POSTS

CLASSES: tuple[dict[str, str], ...] = (
    {"id": "worker", "name": "WORKER", "code": "CLS-01"},
    {"id": "farmer", "name": "FARMER", "code": "CLS-02"},
    {"id": "mechanic", "name": "MECHANIC", "code": "CLS-03"},
    {"id": "it", "name": "IT", "code": "CLS-04"},
    {"id": "administration", "name": "ADMINISTRATION", "code": "CLS-05"},
)

CLASS_BY_ID = {item["id"]: item for item in CLASSES}

@dataclass(frozen=True)
class BunkerClass:
    id: str
    name: str
    code: str


def _digest(seed: str) -> int:
    return int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big")


def normalize_handle(raw: str) -> str:
    value = (raw or "").strip()
    value = re.sub(r"^https?://(www\.)?(x|twitter)\.com/", "", value, flags=re.I)
    value = value.lstrip("@").split("/")[0].split("?")[0].split("#")[0]
    if not HANDLE_RE.fullmatch(value):
        raise ValueError("invalid_handle")
    return value


def _pick_from(pool: tuple[str, ...], used: set[str], *, empty_code: str, exhausted_code: str) -> str:
    if not pool:
        raise ValueError(empty_code)
    start = random.randrange(len(pool))
    for offset in range(len(pool)):
        candidate = pool[(start + offset) % len(pool)]
        key = candidate.lower()
        if key in used:
            continue
        used.add(key)
        return candidate
    raise ValueError(exhausted_code)


def pick_handle(wallet: str, used: set[str]) -> str:
    del wallet
    return _pick_from(
        HANDLES,
        used,
        empty_code="empty_handle_pool",
        exhausted_code="handle_pool_exhausted",
    )


def pick_comment(wallet: str, used: set[str] | None = None) -> str:
    del wallet
    return _pick_from(
        POSTS,
        used if used is not None else set(),
        empty_code="empty_post_pool",
        exhausted_code="post_pool_exhausted",
    )


def resolve_class(choice: str, wallet: str) -> BunkerClass:
    key = (choice or "random").strip().lower()
    if key in {"random", "any", ""}:
        item = CLASSES[_digest(f"class:{wallet.lower()}") % len(CLASSES)]
    else:
        item = CLASS_BY_ID.get(key)
        if item is None:
            raise ValueError("invalid_class")
    return BunkerClass(id=item["id"], name=item["name"], code=item["code"])
