from __future__ import annotations

import random
import threading
import time
from typing import Any

from soft_hub.sdk import CancelledError, HubAccount, HubContext

from plugin.catalog import pick_comment, pick_handle, resolve_class
from plugin.client import ApiRejected, SafeRequestError, normalize_wallet, submit_wallet
from plugin.listing import parse_eth_wei
from plugin.opensea_drop import (
    DropRejected,
    assert_safe_mint_tx,
    build_mint_tx,
    active_stage_watch_message,
    collect_mint_targets,
    find_drop,
    tx_value_wei,
)
from plugin.proxy import proxy_to_url
from plugin.rpc import RpcError
from plugin.txsend import send_prepared_tx, token_id_from_receipt, wait_receipt

PRIMARY_KIND = "account_snapshot"
MINT_KIND = "account_mint"
_REGISTER_KEYS = frozenset({"outcome", "class_name", "x_handle", "queued"})
_MINT_KEYS = frozenset({"outcome", "stage", "minted", "token_id", "tx_hash"})


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
    if context.action_id == "mint":
        return _run_mint(context)
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


def _run_mint(context: HubContext) -> dict[str, Any]:
    timeout_seconds = _int_option(context.options, "timeout_seconds", 30, 5, 120)
    poll_seconds = _int_option(context.options, "poll_interval_seconds", 3, 2, 60)
    watch_minutes = _int_option(context.options, "watch_minutes", 720, 5, 10080)
    try:
        max_mint_wei = parse_eth_wei(
            _str_option(context.options, "max_mint_eth", "0"),
            allow_zero=True,
        )
    except ValueError:
        max_mint_wei = -1

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
            "max_mint_wei": max_mint_wei,
        },
    )
    if max_mint_wei < 0:
        for account in context.accounts:
            _finish_mint(
                context,
                account,
                status="blocked",
                stage="preflight",
                message="Некорректный потолок цены минта",
                result_status="blocked",
                data=_empty_mint("invalid_price", "preflight"),
            )
        return {
            "total": len(context.accounts),
            "succeeded": 0,
            "failed": 0,
            "blocked": len(context.accounts),
            "cancelled": 0,
        }

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

    def _watching(*, extra: str = "") -> None:
        if max_mint_wei == 0:
            message = "Пылесосим бесплатный WL по slug. Public не трогаем"
        else:
            message = f"Ждём WL не дороже {context.options.get('max_mint_eth', '0')} ETH"
        if extra:
            message = extra
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
    deadline = time.monotonic() + deadline_s
    done_slugs: set[str] = set()
    best: dict[str, str] = {}
    _watching()
    try:
        while True:
            context.check_cancelled()
            targets = [
                item
                for item in collect_mint_targets(
                    proxy=lead.secret("proxy"),
                    timeout_seconds=timeout_seconds,
                    max_mint_wei=max_mint_wei,
                )
                if item["slug"] not in done_slugs
            ]
            if targets:
                for target in targets:
                    slug = str(target["slug"])
                    _watching(extra=f"Минтим {slug}. Public не трогаем")
                    outcomes = context.map_accounts(
                        lambda account, target=target: _mint_one(
                            context,
                            account,
                            slug=str(target["slug"]),
                            contract=str(target["contract"]),
                            timeout_seconds=timeout_seconds,
                            poll_seconds=poll_seconds,
                            remain_s=max(30.0, deadline - time.monotonic()),
                            max_mint_wei=max_mint_wei,
                        ),
                        accounts=tuple(ready),
                    )
                    for account, status in zip(ready, outcomes, strict=True):
                        if status == "waiting":
                            continue
                        if best.get(account.id) != "succeeded":
                            best[account.id] = status
                    if any(status == "succeeded" for status in outcomes):
                        done_slugs.add(slug)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if not targets:
                note = ""
                try:
                    note = active_stage_watch_message(
                        find_drop(
                            proxy=lead.secret("proxy"),
                            timeout_seconds=timeout_seconds,
                        )
                    )
                except DropRejected:
                    note = ""
                _watching(extra=note)
                _interruptible_sleep(context, min(float(poll_seconds), remaining))
    except CancelledError:
        for account in ready:
            if account.id in best:
                continue
            context.account_state(
                account.id,
                status="cancelled",
                stage="cancelled",
                message="Ожидание минта остановлено",
            )
            counters["cancelled"] += 1
        for status in best.values():
            counters[status] = counters.get(status, 0) + 1
        return counters

    for account in ready:
        status = best.get(account.id)
        if status:
            counters[status] = counters.get(status, 0) + 1
            continue
        _finish_mint(
            context,
            account,
            status="failed",
            stage="failed",
            message="Подходящий WL по цене не открылся",
            result_status="failed",
            data=_empty_mint("timeout", "wait"),
        )
        counters["failed"] += 1
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
    max_mint_wei: int = 0,
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
            if tx_value_wei(prepared) > max_mint_wei:
                raise DropRejected("mint_too_expensive")
        except DropRejected as err:
            code = str(err)
            if code in {"not_eligible", "drop_inactive"}:
                context.account_state(
                    account.id,
                    status="running",
                    stage="Наблюдает за минтом",
                    progress=0.18,
                    message="Эта стадия не для нас. Ждём свой WL",
                )
                return "waiting"
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
            message="Сминтили ончейн",
            result_status="succeeded",
            data={
                "outcome": "minted",
                "stage": "mint",
                "minted": True,
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
        "mint_too_expensive": "Цена WL выше потолка",
        "invalid_price": "Некорректный потолок цены минта",
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
