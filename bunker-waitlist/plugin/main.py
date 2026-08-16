from __future__ import annotations

import random
import threading
import time
from typing import Any

from soft_hub.sdk import CancelledError, HubAccount, HubContext

from plugin.catalog import pick_comment, pick_handle, resolve_class
from plugin.client import ApiRejected, SafeRequestError, normalize_wallet, submit_wallet
from plugin.hunter import HuntTimeout
from plugin.mint_flow import wait_for_allowlist
from plugin.opensea_drop import DropRejected, assert_safe_mint_tx, build_mint_tx
from plugin.proxy import proxy_to_url
from plugin.rpc import RpcError
from plugin.txsend import send_prepared_tx, token_id_from_receipt, wait_receipt

PRIMARY_KIND = "account_snapshot"
MINT_KIND = "account_mint"
_REGISTER_KEYS = frozenset({"outcome", "class_name", "x_handle", "queued"})
_MINT_KEYS = frozenset(
    {"outcome", "stage", "minted", "listed", "token_id", "tx_hash"}
)


class _RandomAccountPause:
    """Per-account random delay from [min_ms, max_ms], plus optional start stagger."""

    def __init__(self, min_ms: int, max_ms: int) -> None:
        lo = max(0, int(min_ms))
        hi = max(0, int(max_ms))
        if hi < lo:
            lo, hi = hi, lo
        self._lo = lo
        self._hi = hi
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self, context: HubContext) -> int:
        if self._hi <= 0 and self._lo <= 0:
            return 0
        chosen = random.randint(self._lo, self._hi)
        with self._lock:
            now = time.monotonic()
            start_at = max(now, self._next_at)
            self._next_at = start_at + (chosen / 1000.0)
        delay = start_at - time.monotonic()
        if delay > 0:
            _interruptible_sleep(context, delay)
        return chosen


def run(context: HubContext) -> dict[str, Any]:
    if context.action_id == "register_waitlist":
        return _run_register(context)
    if context.action_id in {"mint_and_dump", "mint_and_fixed", "mint_and_percent"}:
        return _run_mint_and_list(context)
    raise ValueError("unsupported_action")


def _run_register(context: HubContext) -> dict[str, Any]:
    timeout_seconds = _int_option(context.options, "timeout_seconds", 30, 5, 120)
    class_choice = _str_option(context.options, "class_choice", "random")
    account_lo, account_hi = _ms_range(
        context.options,
        "account_pause_min_ms",
        "account_pause_max_ms",
        default_lo=800,
        default_hi=2000,
        high=30000,
    )
    account_pause = _RandomAccountPause(account_lo, account_hi)
    used_handles: set[str] = set()
    used_posts: set[str] = set()
    handle_lock = threading.Lock()
    counters = {
        "total": len(context.accounts),
        "succeeded": 0,
        "failed": 0,
        "blocked": 0,
        "cancelled": 0,
    }
    lock = threading.Lock()

    context.log(
        "Старт регистрации BUNKER waitlist",
        data={
            "accounts": len(context.accounts),
            "account_concurrency": int(getattr(context, "account_concurrency", 1) or 1),
            "account_pause_ms": [account_lo, account_hi],
            "timeout_seconds": timeout_seconds,
            "class_choice": class_choice,
        },
    )

    def worker(account: HubAccount) -> str:
        status = _process_register(
            context,
            account,
            timeout_seconds=timeout_seconds,
            class_choice=class_choice,
            account_pause=account_pause,
            used_handles=used_handles,
            used_posts=used_posts,
            handle_lock=handle_lock,
        )
        with lock:
            counters[status] = counters.get(status, 0) + 1
        return status

    context.map_accounts(worker)
    return {
        "total": counters["total"],
        "succeeded": counters.get("succeeded", 0),
        "failed": counters.get("failed", 0),
        "blocked": counters.get("blocked", 0),
        "cancelled": counters.get("cancelled", 0),
    }


def _process_register(
    context: HubContext,
    account: HubAccount,
    *,
    timeout_seconds: int,
    class_choice: str,
    account_pause: _RandomAccountPause,
    used_handles: set[str],
    used_posts: set[str],
    handle_lock: threading.Lock,
) -> str:
    context.check_cancelled()
    account_pause.wait(context)
    context.check_cancelled()
    context.account_state(
        account.id,
        status="running",
        stage="preflight",
        progress=0.05,
        message="Проверяем кошелёк и proxy",
    )

    write_sent = False
    try:
        try:
            wallet = normalize_wallet(account.evm_address)
            proxy = account.secret("proxy")
            proxy_to_url(proxy)
            bunker_class = resolve_class(class_choice, wallet)
            with handle_lock:
                handle = pick_handle(wallet, used_handles)
                comment = pick_comment(wallet, used_posts)
        except (KeyError, ValueError):
            _finish(
                context,
                account,
                status="blocked",
                stage="preflight",
                message="Нет адреса или proxy, либо они неверные",
                result_status="blocked",
                data={
                    "outcome": "blocked",
                    "class_name": "",
                    "x_handle": "",
                    "queued": False,
                },
            )
            return "blocked"

        context.log(
            "Досье собрано без внешних подписок",
            account_id=account.id,
            data={"class_code": bunker_class.code, "quests": 5},
        )
        context.check_cancelled()
        context.account_state(
            account.id,
            status="running",
            stage="automation",
            progress=0.30,
            message="Закрываем квесты и держим позицию",
        )

        context.check_cancelled()
        context.account_state(
            account.id,
            status="running",
            stage="submitting",
            progress=0.55,
            message="Отправляем кошелёк в waitlist",
        )

        write_sent = True
        try:
            result = submit_wallet(
                wallet=wallet,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
                x_username=handle,
                bunker_class=bunker_class,
                comment=comment,
            )
        except SafeRequestError:
            write_sent = False
            _finish(
                context,
                account,
                status="failed",
                stage="failed",
                message="Не удалось отправить заявку",
                result_status="failed",
                data={
                    "outcome": "failed",
                    "class_name": bunker_class.name,
                    "x_handle": handle,
                    "queued": False,
                },
            )
            return "failed"
        except ApiRejected as err:
            write_sent = False
            _finish(
                context,
                account,
                status="failed",
                stage="failed",
                message=_reject_message(err.code),
                result_status="failed",
                data={
                    "outcome": "failed",
                    "class_name": bunker_class.name,
                    "x_handle": handle,
                    "queued": False,
                },
            )
            return "failed"
        write_sent = False

        already = bool(result.already_existed)
        outcome = "already_on_waitlist" if already else "registered"
        message = "Уже в waitlist" if already else "Кошелёк записан в waitlist"
        context.log(
            message,
            account_id=account.id,
            data={"outcome": outcome, "queued": result.queued},
        )
        context.account_state(
            account.id,
            status="running",
            stage="confirming",
            progress=0.90,
            message=message,
        )
        _finish(
            context,
            account,
            status="succeeded",
            stage="completed",
            message=message,
            result_status="succeeded",
            data={
                "outcome": outcome,
                "class_name": bunker_class.name,
                "x_handle": handle,
                "queued": bool(result.queued) and not already,
            },
            progress=1.0,
        )
        return "succeeded"

    except CancelledError:
        context.account_state(
            account.id,
            status="cancelled",
            stage="cancelled",
            message=(
                "Остановка во время отправки — перед повтором проверьте waitlist"
                if write_sent
                else "Остановлено до отправки"
            ),
        )
        return "cancelled"
    except Exception:
        if write_sent:
            _finish(
                context,
                account,
                status="failed",
                stage="failed",
                message="Сбой после отправки — перед повтором проверьте waitlist",
                result_status="failed",
                data={
                    "outcome": "failed",
                    "class_name": "",
                    "x_handle": "",
                    "queued": False,
                },
            )
            return "failed"
        _finish(
            context,
            account,
            status="failed",
            stage="failed",
            message="Не удалось отправить заявку",
            result_status="failed",
            data={
                "outcome": "failed",
                "class_name": "",
                "x_handle": "",
                "queued": False,
            },
        )
        return "failed"


def _run_mint_and_list(context: HubContext) -> dict[str, Any]:
    timeout_seconds = _int_option(context.options, "timeout_seconds", 30, 5, 120)
    poll_seconds = _int_option(context.options, "poll_interval_seconds", 3, 2, 60)
    watch_minutes = _int_option(context.options, "watch_minutes", 180, 5, 720)
    list_mode = {
        "mint_and_dump": "dump",
        "mint_and_fixed": "fixed",
        "mint_and_percent": "percent",
    }[context.action_id]
    profit_percent = _int_option(context.options, "profit_percent", 20, 0, 500) if list_mode == "percent" else 0
    fixed_eth = str(context.options.get("list_price_eth", "0")) if list_mode == "fixed" else "0"

    counters = {
        "total": len(context.accounts),
        "succeeded": 0,
        "failed": 0,
        "blocked": 0,
        "cancelled": 0,
    }
    ready: list[HubAccount] = []

    context.log(
        "Старт ожидания WL-минта BUNKER",
        data={
            "accounts": len(context.accounts),
            "watch_minutes": watch_minutes,
            "poll_interval_seconds": poll_seconds,
            "list_mode": list_mode,
        },
    )

    for account in context.accounts:
        context.check_cancelled()
        context.account_state(
            account.id,
            status="running",
            stage="preflight",
            progress=0.05,
            message="Проверяем кошелёк и proxy",
        )
        try:
            normalize_wallet(account.evm_address)
            proxy = account.secret("proxy")
            proxy_to_url(proxy)
            account.secret("evm_private_key")
        except (KeyError, ValueError):
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="preflight",
                message="Нет приватника или proxy",
                result_status="blocked",
                data=_empty_mint("blocked", "preflight"),
            )
            counters["blocked"] += 1
            continue
        ready.append(account)

    if not ready:
        return counters

    def _watching(snapshot: dict[str, Any] | None = None) -> None:
        reason = str((snapshot or {}).get("reason") or "")
        if reason == "unpublished":
            message = "Коллекция ещё не вышла — наблюдаем за минтом"
        else:
            message = "Наблюдаем за минтом. Ждём открытие WL"
        for account in ready:
            context.account_state(
                account.id,
                status="running",
                stage="Наблюдает за минтом",
                progress=0.18,
                message=message,
            )

    lead = ready[0]
    deadline_s = watch_minutes * 60
    _watching()
    try:
        hunted = wait_for_allowlist(
            proxy=lead.secret("proxy"),
            timeout_seconds=timeout_seconds,
            wallet=normalize_wallet(lead.evm_address),
            deadline_s=deadline_s,
            poll_s=poll_seconds,
            check_cancelled=context.check_cancelled,
            on_wait=_watching,
        )
    except HuntTimeout:
        for account in ready:
            _finish_mint(
                context,
                account,
                status="failed",
                stage="failed",
                message="Окно WL не открылось до конца ожидания",
                result_status="failed",
                data=_empty_mint("timeout", "wait"),
            )
            counters["failed"] += 1
        return counters
    except ValueError as err:
        code = str(err)
        message = _reject_message(code)
        for account in ready:
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="blocked",
                message=message,
                result_status="blocked",
                data=_empty_mint(code, "public" if code == "public_stage" else "ended"),
            )
            counters["blocked"] += 1
        return counters
    except CancelledError:
        for account in ready:
            context.account_state(
                account.id,
                status="cancelled",
                stage="cancelled",
                message="Ожидание минта остановлено",
            )
            counters["cancelled"] += 1
        return counters

    slug = str(hunted.get("slug") or "")
    contract = str(hunted.get("contract") or "")
    remain = max(30.0, float(deadline_s) / 3)

    def worker(account: HubAccount) -> str:
        return _mint_one(
            context,
            account,
            slug=slug,
            contract=contract,
            timeout_seconds=timeout_seconds,
            poll_seconds=poll_seconds,
            remain_s=remain,
            list_mode=list_mode,
            profit_percent=profit_percent,
            fixed_eth=fixed_eth,
        )

    outcomes = context.map_accounts(worker, accounts=tuple(ready))
    for status in outcomes:
        counters[status] = counters.get(status, 0) + 1
    return counters


def _mint_one(
    context: HubContext,
    account: HubAccount,
    *,
    slug: str,
    contract: str,
    timeout_seconds: int,
    poll_seconds: int,
    remain_s: float,
    list_mode: str,
    profit_percent: int,
    fixed_eth: str,
) -> str:
    context.check_cancelled()
    wallet = normalize_wallet(account.evm_address)
    proxy = account.secret("proxy")
    context.account_state(
        account.id,
        status="running",
        stage="Собирает минт",
        progress=0.40,
        message="Берём calldata WL-минта. Сайт OpenSea не открываем",
    )
    try:
        if not slug:
            _finish_mint(
                context,
                account,
                status="failed",
                stage="failed",
                message="Коллекция ещё без slug — минтить нечего",
                result_status="failed",
                data=_empty_mint("no_slug", "allowlist"),
            )
            return "failed"
        try:
            prepared = build_mint_tx(
                slug=slug,
                wallet=wallet,
                proxy=proxy,
                timeout_seconds=timeout_seconds,
            )
            assert_safe_mint_tx(prepared, contract=contract)
        except DropRejected as err:
            code = str(err)
            _finish_mint(
                context,
                account,
                status="failed",
                stage="failed",
                message=_reject_message(code),
                result_status="failed",
                data=_empty_mint(code, "allowlist"),
            )
            return "failed"

        context.check_cancelled()
        context.account_state(
            account.id,
            status="running",
            stage="Минтит ончейн",
            progress=0.70,
            message="Подписываем и шлём минт в сеть",
        )
        tx_hash = send_prepared_tx(
            private_key=account.secret("evm_private_key"),
            to=prepared["to"],
            data=prepared["data"],
            value=prepared["value"],
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        receipt = wait_receipt(
            tx_hash=tx_hash,
            proxy=proxy,
            timeout_seconds=timeout_seconds,
        )
        if str(receipt.get("status") or "").lower() in {"0x0", "0"}:
            _finish_mint(
                context,
                account,
                status="failed",
                stage="failed",
                message="Минт ревертнулся в сети",
                result_status="failed",
                data={**_empty_mint("reverted", "mint"), "tx_hash": tx_hash},
            )
            return "failed"
        token_id = token_id_from_receipt(receipt)
        _finish_mint(
            context,
            account,
            status="succeeded",
            stage="completed",
            message="Сминтили ончейн. Ордер на OpenSea — когда откроется продажа",
            result_status="succeeded",
            data={
                "outcome": "minted",
                "stage": "mint",
                "minted": True,
                "listed": False,
                "token_id": token_id,
                "tx_hash": tx_hash,
            },
            progress=1.0,
        )
        return "succeeded"
    except RpcError:
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message="Сеть не приняла транзакцию",
            result_status="failed",
            data=_empty_mint("rpc_error", "mint"),
        )
        return "failed"
    except CancelledError:
        context.account_state(
            account.id,
            status="cancelled",
            stage="cancelled",
            message="Минт остановлен. Проверьте explorer перед повтором",
        )
        return "cancelled"
    except Exception:
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message="Минт не выполнен. Перед повтором проверьте explorer",
            result_status="failed",
            data=_empty_mint("failed", "failed"),
        )
        return "failed"


def _empty_mint(outcome: str, stage: str) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "stage": stage,
        "minted": False,
        "listed": False,
        "token_id": "",
        "tx_hash": "",
    }


def _finish_mint(
    context: HubContext,
    account: HubAccount,
    *,
    status: str,
    stage: str,
    message: str,
    result_status: str,
    data: dict[str, Any],
    progress: float | None = None,
) -> None:
    _finish(
        context,
        account,
        status=status,
        stage=stage,
        message=message,
        result_status=result_status,
        data=data,
        progress=progress,
        keys=_MINT_KEYS,
        kind=MINT_KIND,
    )


def _reject_message(code: str) -> str:
    mapping = {
        "already_registered": "Кошелёк или X уже в waitlist",
        "rate_limited": "Слишком много заявок с этого proxy — подождите и уменьшите потоки",
        "bad_request": "Сервис отклонил заявку",
        "invalid_submission": "Сервис не принял состав заявки",
        "forbidden": "Сервис запретил заявку",
        "server_error": "Сервис временно недоступен",
        "invalid_wallet": "Адрес кошелька не принят",
        "sheets_blocked": "Очередь проекта недоступна",
        "unexpected_page": "Страница входа выглядит иначе, чем ожидалось",
        "unexpected_http_status": "Сервис вернул неожиданный ответ",
        "timeout": "Истёк таймаут",
        "proxy_error": "Proxy не отвечает",
        "request_failed": "Сеть не ответила",
        "public_stage": "Открыт public. WL-минт не отправляем",
        "stage_ended": "Окно минта уже закрыто",
        "hunt_timeout": "Окно WL не открылось до конца ожидания",
        "no_signature": "Нет подписи OpenSea на этот кошелёк",
        "mint_payload_unknown": "Формат минта ещё не закреплён",
        "api_key": "OpenSea не выдала ключ API",
        "drop_request": "Не удалось прочитать drop на OpenSea",
        "mint_request": "OpenSea не собрала транзакцию минта",
        "drop_inactive": "Drop ещё не активен",
        "not_eligible": "Кошелёк не в allowlist этой стадии",
        "bad_target": "OpenSea вернула чужой контракт — не шлём",
        "bad_calldata": "OpenSea вернула пустой calldata",
        "public_calldata": "Это public-минт. Не отправляем",
        "wrong_chain": "Это не Robinhood Chain",
        "no_slug": "Нет slug коллекции",
        "reverted": "Минт ревертнулся в сети",
        "rpc_error": "RPC не принял транзакцию",
    }
    return mapping.get(code, "Заявка не принята")


def _finish(
    context: HubContext,
    account: HubAccount,
    *,
    status: str,
    stage: str,
    message: str,
    result_status: str,
    data: dict[str, Any],
    progress: float | None = None,
    keys: frozenset[str] = _REGISTER_KEYS,
    kind: str = PRIMARY_KIND,
) -> None:
    safe_data = {k: data[k] for k in keys if k in data}
    context.result(
        f"{account.label}: {message}",
        kind=kind,
        status=result_status,
        account_id=account.id,
        data=safe_data,
    )
    kwargs: dict[str, Any] = {
        "status": status,
        "stage": stage,
        "message": message,
    }
    if progress is not None:
        kwargs["progress"] = progress
    context.account_state(account.id, **kwargs)


def _int_option(
    options: dict[str, Any],
    name: str,
    default: int,
    low: int,
    high: int,
) -> int:
    value = options.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"invalid_{name}")
    if not low <= value <= high:
        raise ValueError(f"invalid_{name}")
    return value


def _str_option(options: dict[str, Any], name: str, default: str) -> str:
    value = options.get(name, default)
    if not isinstance(value, str):
        raise ValueError(f"invalid_{name}")
    value = value.strip()
    if not value:
        raise ValueError(f"invalid_{name}")
    return value


def _ms_range(
    options: dict[str, Any],
    lo_name: str,
    hi_name: str,
    *,
    default_lo: int,
    default_hi: int,
    high: int,
) -> tuple[int, int]:
    lo = _int_option(options, lo_name, default_lo, 0, high)
    hi = _int_option(options, hi_name, default_hi, 0, high)
    if hi < lo:
        lo, hi = hi, lo
    return lo, hi


def _interruptible_sleep(context: HubContext, seconds: float) -> None:
    if seconds <= 0:
        return
    end = time.monotonic() + seconds
    while True:
        context.check_cancelled()
        remaining = end - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))
