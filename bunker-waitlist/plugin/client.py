from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import httpx

from plugin.catalog import BunkerClass
from plugin.proxy import proxy_to_url

SITE_ORIGIN = "https://thebunkerhood.com"
SITE_REFERER = "https://thebunkerhood.com/enter"
SUBMIT_PATH = "/api/submit"
ENTER_PATH = "/enter"

WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)

# First UI verify always fails; second verify is what the site sends.
QUEST_ATTEMPTS = 2

_ALREADY_TOKENS = (
    "already",
    "exist",
    "duplicate",
    "recorded",
    "already registered",
    "already_exists",
)


class SafeRequestError(Exception):
    """Network/proxy failure without secret-bearing message text."""

    def __init__(self, code: str = "request_failed") -> None:
        self.code = code
        super().__init__(code)


class ApiRejected(Exception):
    """API answered without a usable waitlist result."""

    def __init__(self, code: str, http_status: int = 0) -> None:
        self.code = code
        self.http_status = http_status
        super().__init__(code)


@dataclass(frozen=True)
class SiteSnapshot:
    http_status: int
    ok: bool


@dataclass(frozen=True)
class SubmitResult:
    http_status: int
    already_existed: bool
    queued: bool
    ticket: str


def normalize_wallet(raw: str) -> str:
    value = (raw or "").strip()
    if value and not value.startswith("0x") and not value.startswith("0X"):
        value = "0x" + value
    if not WALLET_RE.match(value):
        raise ValueError("invalid_wallet")
    return "0x" + value[2:]


def _client_headers(*, json_body: bool = False) -> dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Origin": SITE_ORIGIN,
        "Referer": SITE_REFERER,
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _http_client(*, proxy: str, timeout_seconds: int) -> httpx.Client:
    return httpx.Client(
        proxy=proxy_to_url(proxy),
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        headers=_client_headers(),
    )


def fetch_site(*, proxy: str, timeout_seconds: int) -> SiteSnapshot:
    try:
        with _http_client(proxy=proxy, timeout_seconds=timeout_seconds) as client:
            response = client.get(f"{SITE_ORIGIN}{ENTER_PATH}")
            status_code = int(response.status_code)
            text = response.text or ""
    except httpx.TimeoutException:
        raise SafeRequestError("timeout") from None
    except httpx.ProxyError:
        raise SafeRequestError("proxy_error") from None
    except httpx.HTTPError:
        raise SafeRequestError("request_failed") from None

    if status_code != 200:
        raise ApiRejected(_status_code_name(status_code), status_code)
    if "ENTER THE BUNKER" not in text and "ACCESS CODE" not in text:
        raise ApiRejected("unexpected_page", status_code)
    return SiteSnapshot(http_status=status_code, ok=True)


def submit_wallet(
    *,
    wallet: str,
    proxy: str,
    timeout_seconds: int,
    x_username: str,
    bunker_class: BunkerClass,
    comment: str,
) -> SubmitResult:
    wallet = normalize_wallet(wallet)
    payload = {
        "submitted_at": _utc_now(),
        "x_username": x_username,
        "wallet_address": wallet,
        "class_name": bunker_class.name,
        "class": bunker_class.name,
        "class_code": bunker_class.code,
        "comment": comment,
        "follow_verified": True,
        "like_verified": True,
        "comment_verified": True,
        "article_verified": True,
        "hold_confirmed": True,
        "hold_position": True,
        "follow_attempts": QUEST_ATTEMPTS,
        "like_attempts": QUEST_ATTEMPTS,
        "comment_attempts": QUEST_ATTEMPTS,
        "article_attempts": QUEST_ATTEMPTS,
    }

    try:
        with httpx.Client(
            proxy=proxy_to_url(proxy),
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=True,
            headers=_client_headers(json_body=True),
        ) as client:
            response = client.post(f"{SITE_ORIGIN}{SUBMIT_PATH}", json=payload)
            status_code = int(response.status_code)
            try:
                data = response.json()
            except Exception:
                data = {}
    except httpx.TimeoutException:
        raise SafeRequestError("timeout") from None
    except httpx.ProxyError:
        raise SafeRequestError("proxy_error") from None
    except httpx.HTTPError:
        raise SafeRequestError("request_failed") from None

    if not isinstance(data, dict):
        data = {}

    if status_code in {200, 201, 202} and data.get("ok") is True:
        ticket = _safe_ticket(data.get("message_id"))
        return SubmitResult(
            http_status=status_code,
            already_existed=False,
            queued=bool(data.get("queued")),
            ticket=ticket,
        )

    if status_code in {409, 422} or _body_means_already(data):
        return SubmitResult(
            http_status=status_code,
            already_existed=True,
            queued=False,
            ticket="",
        )

    raise ApiRejected(_status_code_name(status_code, data), status_code)


def _utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _safe_ticket(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    value = raw.strip()
    if not value or len(value) > 80:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", value):
        return ""
    return value


def _body_means_already(data: dict[str, Any]) -> bool:
    for key in ("already", "exists", "alreadyExists", "already_registered", "duplicate"):
        if data.get(key) is True:
            return True
    for key in ("message", "error", "code", "reason"):
        raw = data.get(key)
        if not isinstance(raw, str):
            continue
        low = raw.lower()
        if any(token in low for token in _ALREADY_TOKENS):
            return True
    return False


def _status_code_name(status_code: int, data: dict[str, Any] | None = None) -> str:
    if data:
        raw = data.get("error")
        if isinstance(raw, str):
            low = raw.lower()
            if "invalid_submission" in low or "invalid submission" in low:
                return "invalid_submission"
            if "google_sheets" in low:
                return "sheets_blocked"
            if "method_not_allowed" in low:
                return "method_not_allowed"
            if any(token in low for token in _ALREADY_TOKENS):
                return "already_registered"
    if status_code == 409:
        return "already_registered"
    if status_code == 429:
        return "rate_limited"
    if status_code == 400:
        return "bad_request"
    if status_code == 403:
        return "forbidden"
    if 500 <= status_code < 600:
        return "server_error"
    return "unexpected_http_status"
