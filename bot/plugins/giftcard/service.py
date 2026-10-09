"""Плагин Gift Card: заказ Playerok → покупка карты у поставщика → код покупателю.
Поставщики: FazerCards (client.py) и AppRoute (approute.py) — у каждой привязки лота свой.

Закрытый: работает только для аккаунтов администраторов (access.is_admin), так как
покупки идут с баланса владельца по ключу FAZER_API_KEY из переменных окружения.

Состояния GiftcardOrder.status:
  PROCESSING  строка создана и сохранена ДО запроса покупки
  AWAITING    поставщик создал заказ, кодов ещё нет — опрашиваем GET /orders/{id}
  BOUGHT      коды получены и сохранены (зашифрованно), но ещё не отправлены в чат
  DELIVERED   коды отправлены покупателю — дальше работает обычное автоподтверждение
  UNKNOWN     итог покупки неизвестен (тайм-аут/5xx) — повтор ТОЛЬКО с тем же
              Idempotency-Key: по документации FazerCards он вернёт исходный заказ
              и не спишет деньги повторно
  FAILED      поставщик однозначно отказал (нет товара, мало баланса и т.п.)
  NEEDS_CHECK нужен человек: непонятный формат кода или сделка закрыта до выдачи
Один заказ = одна строка (уникальность seller+deal), поэтому дважды не покупаем.
"""

from __future__ import annotations

import html
import logging
import os
from datetime import datetime, timedelta
from typing import Any

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...crypto import TokenCipher
from ...db import DealState, GiftcardMap, GiftcardOrder, Seller
from ...logs import tag
from ...playerok import Deal, PlayerokClient, PlayerokError
from ...services import features as ft
from ...services.notifications import notify
from ..access import is_admin
from .client import (
    ORDER_ID_RE,
    FazerAuthError,
    FazerCardsClient,
    FazerError,
    FazerUnknownResult,
    api_key_from_env,
)
from .approute import AppRouteClient, norm_status, reference_for, voucher_codes

log = logging.getLogger(__name__)

PLUGIN_KEY = "giftcard"
ENABLED_KEY = "giftcard_enabled"
ENABLED_AT_KEY = "giftcard_enabled_at"
MAX_ATTEMPTS = 5  # повторов покупки с тем же ключом при неизвестном итоге

DONE_TEXT = (
    "Спасибо за покупку, {Имя_Клиента}! Ваш код:\n{Код}\n\n"
    "Пожалуйста, подтвердите получение заказа 🙏"
)
FAIL_TEXT = "Возникла заминка с автоматической выдачей, продавец уже уведомлён и выдаст вручную."

FAILED_ORDER_STATUSES = {"failed", "cancelled", "canceled", "refunded", "rejected", "error", "expired"}
# Поля карточки, которые считаем кодом (по документации: «Completed order includes
# `cards` (codes)»; точная структура элемента не описана — берём только явные поля).
CODE_FIELDS = (
    ("code", ""), ("card_code", ""), ("key", ""), ("redeem_code", ""), ("activation_code", ""),
    ("pin", "PIN"), ("serial", "Серийный номер"), ("card_number", "Номер карты"),
)

# Только для тестов: подмена HTTP-транспорта FazerCards и AppRoute.
TRANSPORT = None
AR_TRANSPORT = None

PROVIDERS = {"fazer": "FazerCards", "approute": "AppRoute"}
AR_KEY_SETTING = "approute_api_key_enc"
AR_REGION_SETTING = "approute_region"  # io — международный, ru — для России


def provider_of(obj) -> str:
    return getattr(obj, "provider", None) or "fazer"


def _now() -> datetime:
    return datetime.utcnow()


KEY_SETTING = "giftcard_api_key_enc"


def make_client(api_key: str) -> FazerCardsClient:
    return FazerCardsClient(api_key, transport=TRANSPORT)


async def get_api_key(session: AsyncSession, tg: int, provider: str = "fazer") -> str:
    """Ключ, введённый в боте (хранится зашифрованным), иначе из окружения
    (FAZER_API_KEY / APPROUTE_API_KEY)."""
    setting = AR_KEY_SETTING if provider == "approute" else KEY_SETTING
    enc = await ft.get_setting(session, tg, setting, "")
    if enc:
        try:
            return _cipher().decrypt(enc)
        except Exception:
            log.warning("%s GIFTCARD API_ERROR: сохранённый ключ %s не расшифровывается", tag(tg), provider)
    if provider == "approute":
        return os.getenv("APPROUTE_API_KEY", "").strip()
    return api_key_from_env()


async def set_api_key(session: AsyncSession, tg: int, key: str, provider: str = "fazer") -> None:
    setting = AR_KEY_SETTING if provider == "approute" else KEY_SETTING
    await ft.set_setting(session, tg, setting, _cipher().encrypt(key) if key else "")


async def make_approute(session: AsyncSession, tg: int) -> AppRouteClient:
    region = await ft.get_setting(session, tg, AR_REGION_SETTING, "io")
    return AppRouteClient(await get_api_key(session, tg, "approute"), region=region, transport=AR_TRANSPORT)


def _cipher() -> TokenCipher:
    return TokenCipher(os.environ.get("SECRET_KEY", ""))


class UnknownCodeFormat(Exception):
    pass


# ----- разбор ответа поставщика -----


def provider_order_id(order: dict[str, Any]) -> str | None:
    for key in ("id", "order_id", "public_id"):
        v = order.get(key)
        if isinstance(v, str) and ORDER_ID_RE.match(v):
            return v
    return None


def extract_codes(order: dict[str, Any]) -> list[str]:
    """Коды из заказа. [] — кодов пока нет. UnknownCodeFormat — коды есть,
    но в незнакомом виде (тогда не выдаём наугад, а зовём человека)."""
    cards = order.get("cards")
    if cards is None or cards == []:
        return []
    if not isinstance(cards, list):
        raise UnknownCodeFormat(f"cards: {type(cards).__name__}")
    codes = []
    for card in cards:
        if isinstance(card, str) and card.strip():
            codes.append(card.strip())
            continue
        if isinstance(card, dict):
            parts = []
            for field, label in CODE_FIELDS:
                v = card.get(field)
                if isinstance(v, (str, int)) and str(v).strip():
                    parts.append(f"{label}: {v}" if label else str(v).strip())
            if parts:
                codes.append("\n".join(parts))
                continue
            raise UnknownCodeFormat("поля карты: " + ", ".join(sorted(map(str, card.keys())))[:200])
        raise UnknownCodeFormat(f"элемент cards: {type(card).__name__}")
    return codes


# Поля суммы заказа. Формат заказа в документации FazerCards не описан, поэтому берём
# только явные денежные поля; нет — цена номинала из каталога (помечается «≈»).
COST_FIELDS = ("price_usd", "total_usd", "amount_usd", "cost_usd", "total", "amount", "price", "cost", "sum")


def extract_cost(order: dict[str, Any]) -> float | None:
    for field in COST_FIELDS:
        v = order.get(field)
        if isinstance(v, dict):
            v = v.get("usd") or v.get("amount") or v.get("value")
        try:
            value = float(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            continue
        if value is not None and value >= 0:
            return value
    return None


def order_failed(order: dict[str, Any]) -> bool:
    return str(order.get("status") or "").lower() in FAILED_ORDER_STATUSES


# ----- привязки лотов -----


async def find_mapping(session: AsyncSession, tg: int, deal: Deal) -> GiftcardMap | None:
    from ...services.automation import norm_lot  # ленивый импорт: automation импортирует нас

    maps = list(await session.scalars(select(GiftcardMap).where(GiftcardMap.seller_tg_id == tg)))
    name = norm_lot(deal.item_name)
    ids = {x for x in (deal.item_id, deal.item_slug) if x}
    best: tuple[int, GiftcardMap] | None = None
    for m in maps:
        key = m.lot_key.strip()
        if key in ids or (deal.item_slug and key.rstrip("/").endswith("/" + deal.item_slug)):
            return m
        k = norm_lot(key)
        if k and k == name:
            return m
        # целые слова, не короче 6 символов — чтобы «100» не цеплялось к «1000»
        if len(k) >= 6 and f" {k} " in f" {name} ":
            if best is None or len(k) > best[0]:
                best = (len(k), m)
    return best[1] if best else None


# ----- ручная выдача по уже оплаченным заказам -----

MANUAL_WINDOW = timedelta(hours=24)


def _deal_like(st: DealState):
    """Объект с полями, которые нужны find_mapping, из сохранённой сделки."""
    from types import SimpleNamespace

    url = st.item_url or ""
    slug = url.split("/products/", 1)[1].strip("/") if "/products/" in url else ""
    return SimpleNamespace(item_name=st.item_name or "", item_id=st.item_id or "", item_slug=slug)


async def pending_paid_deals(session: AsyncSession, tg: int) -> list[tuple[DealState, GiftcardMap]]:
    """Оплаченные (PAID) заказы за 24 ч на привязанные лоты, по которым Gift Card ещё
    не покупалась — например, пришли, пока плагин был выключен."""
    since = _now() - MANUAL_WINDOW
    states = list(await session.scalars(
        select(DealState).where(
            DealState.seller_tg_id == tg,
            DealState.status == "PAID",
            DealState.first_seen_at >= since,
        ).order_by(DealState.first_seen_at.desc())
    ))
    taken = set(await session.scalars(
        select(GiftcardOrder.deal_id).where(GiftcardOrder.seller_tg_id == tg)
    ))
    result = []
    for st in states:
        if st.deal_id in taken or st.delivered:
            continue
        mapping = await find_mapping(session, tg, _deal_like(st))
        if mapping is not None:
            result.append((st, mapping))
    return result


async def start_manual(session: AsyncSession, tg: int, deal_id: str) -> str:
    """Ставит заказ в работу вручную: создаёт строку PROCESSING — ближайший опрос
    (≤30 с) купит карту с тем же защитным Idempotency-Key и выдаст код."""
    for st, mapping in await pending_paid_deals(session, tg):
        if st.deal_id != deal_id:
            continue
        session.add(GiftcardOrder(
            seller_tg_id=tg,
            deal_id=st.deal_id,
            chat_id=st.chat_id,
            item_name=(st.item_name or "")[:255],
            buyer=(st.buyer or "")[:64],
            category_id=mapping.category_id,
            card_id=mapping.card_id,
            quantity=mapping.quantity or 1,
            idem_key=f"playerok-{tg}-{st.deal_id}",
            provider=provider_of(mapping),
            status="PROCESSING",
            updated_at=_now(),
        ))
        await session.commit()
        log.info("%s GIFTCARD ORDER_RECEIVED (вручную) сделка %s «%s»", tag(tg), st.deal_id, st.item_name)
        return "ok"
    return "Заказ уже в работе, выдан или больше не оплачен."


# ----- основной вход из automation._process_deal -----


async def on_deal(
    bot: Bot,
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    deal: Deal,
    *,
    first_seen: datetime | None,
) -> str | None:
    """None — сделка не наша (работает обычная автовыдача).
    "delivered" — коды выданы; "handled" — сделка наша, но ещё не выдана."""
    tg = seller.tg_id
    if not is_admin(tg):
        return None
    order = await session.scalar(
        select(GiftcardOrder).where(GiftcardOrder.seller_tg_id == tg, GiftcardOrder.deal_id == deal.id)
    )
    if order is None:
        if deal.status != "PAID":
            return None
        if not await ft.get_flag(session, tg, ENABLED_KEY, False):
            return None
        mapping = await find_mapping(session, tg, deal)
        if mapping is None:
            return None
        if not await _fresh_enough(session, tg, first_seen):
            log.info("%s GIFTCARD сделка %s появилась до включения плагина — пропуск", tag(tg), deal.id)
            return None
        prov = provider_of(mapping)
        if not await get_api_key(session, tg, prov):
            log.warning("%s GIFTCARD API_ERROR: не задан ключ %s, сделка %s не обработана", tag(tg), prov, deal.id)
            await notify(
                bot, session, tg, "problem",
                f"🎁 Gift Card: пришёл заказ на привязанный лот, но не задан API-ключ {PROVIDERS[prov]}. "
                "Выдай вручную и введи ключ: /giftcard → «🔑».",
            )
            return None
        order = GiftcardOrder(
            seller_tg_id=tg,
            deal_id=deal.id,
            chat_id=deal.chat_id,
            item_name=deal.item_name[:255],
            buyer=(deal.buyer_username or "")[:64],
            category_id=mapping.category_id,
            card_id=mapping.card_id,
            quantity=mapping.quantity or 1,
            idem_key=f"playerok-{tg}-{deal.id}",
            provider=provider_of(mapping),
            status="PROCESSING",
            updated_at=_now(),
        )
        session.add(order)
        # Фиксируем намерение ДО покупки: после перезапуска строка уже есть, и повтор
        # пойдёт с тем же Idempotency-Key — второй карты не будет.
        await session.commit()
        log.info("%s GIFTCARD ORDER_RECEIVED сделка %s «%s» → %s/%s ×%s", tag(tg), deal.id,
                 deal.item_name, order.category_id, order.card_id, order.quantity)
    if order.chat_id is None and deal.chat_id:
        order.chat_id = deal.chat_id

    if order.status == "DELIVERED":
        return "delivered"
    if deal.status != "PAID":
        await _deal_closed(bot, session, order, deal)
        return "handled"
    await advance(bot, session, seller, client, order, deal)
    return "delivered" if order.status == "DELIVERED" else "handled"


async def _fresh_enough(session: AsyncSession, tg: int, first_seen: datetime | None) -> bool:
    """Не покупаем под заказы, которые бот увидел раньше, чем включили плагин."""
    raw = await ft.get_setting(session, tg, ENABLED_AT_KEY, "")
    try:
        enabled_at = datetime.fromisoformat(raw) if raw else None
    except ValueError:
        enabled_at = None
    if enabled_at is None or first_seen is None:
        return True
    return first_seen >= enabled_at - timedelta(seconds=5)


async def _deal_closed(bot: Bot, session: AsyncSession, order: GiftcardOrder, deal: Deal) -> None:
    """Сделку отменили/закрыли раньше, чем код дошёл до покупателя."""
    if order.status in ("FAILED", "NEEDS_CHECK"):
        return
    tg = order.seller_tg_id
    bought = order.status in ("BOUGHT", "AWAITING", "UNKNOWN")
    order.status = "NEEDS_CHECK" if bought else "FAILED"
    order.error = f"сделка в статусе {deal.status} до выдачи"
    order.updated_at = _now()
    log.warning("%s GIFTCARD DELIVERY_FAILED сделка %s: %s (покупка: %s)", tag(tg), deal.id,
                order.error, "возможно, была" if bought else "не было")
    if bought and not order.admin_told:
        order.admin_told = True
        await notify(
            bot, session, tg, "problem",
            f"🎁 Gift Card: заказ «{html.escape(order.item_name)}» стал {html.escape(deal.status)} "
            "до выдачи, а карта уже могла быть куплена. Код не отправлен — проверь в /giftcard → «Заказы».",
        )


async def advance(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, order: GiftcardOrder, deal: Deal | None
) -> None:
    """Двигает заказ по состояниям. Безопасно вызывать на каждом опросе."""
    tg = order.seller_tg_id
    if provider_of(order) == "approute":
        await _advance_approute(bot, session, seller, client, order, deal)
        return
    if order.status in ("PROCESSING", "UNKNOWN"):
        if order.attempts >= MAX_ATTEMPTS:
            return
        order.attempts += 1
        order.updated_at = _now()
        await session.commit()  # попытка учтена до запроса
        log.info("%s GIFTCARD PROCESSING сделка %s, попытка %s", tag(tg), order.deal_id, order.attempts)
        log.info("%s GIFTCARD API_REQUEST покупка %s/%s ×%s", tag(tg), order.category_id, order.card_id, order.quantity)
        try:
            async with make_client(await get_api_key(session, tg)) as api:
                result = await api.order_giftcard(order.category_id, order.card_id, order.quantity, order.idem_key)
        except FazerUnknownResult as e:
            order.status = "UNKNOWN"
            order.error = str(e)[:500]
            log.warning("%s GIFTCARD API_ERROR сделка %s: итог неизвестен — %s", tag(tg), order.deal_id, e)
            if order.attempts >= MAX_ATTEMPTS:
                await _tell_admin(bot, session, order,
                                  f"итог покупки неизвестен после {order.attempts} попыток: {e}. "
                                  "Повторная покупка не делалась. Проверь заказы в кабинете FazerCards.")
            return
        except FazerError as e:
            await _fail(bot, session, seller, client, order, e)
            return
        failed = _apply_order(order, result)
    elif order.status == "AWAITING":
        if not order.provider_order_id:
            order.status = "UNKNOWN"
            return
        try:
            async with make_client(await get_api_key(session, tg)) as api:
                result = await api.get_order(order.provider_order_id)
        except FazerError as e:
            log.info("%s GIFTCARD API_ERROR проверка заказа %s: %s", tag(tg), order.provider_order_id, e)
            return
        failed = _apply_order(order, result)
    else:
        failed = False

    if failed:
        await _fail(bot, session, seller, client, order, FazerError("поставщик отменил заказ"))
        return
    if order.status == "NEEDS_CHECK" and not order.admin_told:
        await _tell_admin(bot, session, order,
                          f"коды пришли в незнакомом формате ({order.error}). Покупателю ничего не отправлено.")
        return
    if order.status == "BOUGHT":
        await _deliver(bot, session, seller, client, order, deal)


async def _advance_approute(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, order: GiftcardOrder, deal: Deal | None
) -> None:
    """AppRoute: POST /orders с referenceId из ключа заказа (повтор вернёт тот же заказ,
    второй раз не спишет); коды в ответе бывают скрыты «****1234» — тогда
    GET /orders?referenceId=…&unhide=true."""
    tg = order.seller_tg_id
    ref = reference_for(order.idem_key)
    api = await make_approute(session, tg)
    try:
        if order.status in ("PROCESSING", "UNKNOWN"):
            if order.attempts >= MAX_ATTEMPTS:
                return
            order.attempts += 1
            order.updated_at = _now()
            await session.commit()  # попытка учтена до запроса
            log.info("%s GIFTCARD API_REQUEST AppRoute покупка %s/%s ×%s, попытка %s", tag(tg),
                     order.category_id, order.card_id, order.quantity, order.attempts)
            try:
                result: dict[str, Any] | None = await api.order(order.category_id, order.card_id, order.quantity, ref)
            except FazerUnknownResult as e:
                order.status = "UNKNOWN"
                order.error = str(e)[:500]
                log.warning("%s GIFTCARD API_ERROR сделка %s: итог неизвестен — %s", tag(tg), order.deal_id, e)
                if order.attempts >= MAX_ATTEMPTS:
                    await _tell_admin(bot, session, order,
                                      f"итог покупки неизвестен после {order.attempts} попыток: {e}. "
                                      "Повторная покупка не делалась. Проверь заказы в кабинете AppRoute.")
                return
            except FazerError as e:
                await _fail(bot, session, seller, client, order, e)
                return
        elif order.status == "AWAITING":
            try:
                result = await api.find_order(ref)
            except FazerError as e:
                log.info("%s GIFTCARD API_ERROR проверка заказа AppRoute %s: %s", tag(tg), order.deal_id, e)
                return
            if result is None:
                return
        else:
            result = None
        if result is not None:
            codes, masked = _apply_approute(order, result)
            if masked and order.status not in ("FAILED",):
                # Полные коды — отдельным запросом с unhide=true (он отмечает коды полученными).
                try:
                    full = await api.find_order(ref)
                except FazerError as e:
                    log.info("%s GIFTCARD API_ERROR коды AppRoute %s: %s", tag(tg), order.deal_id, e)
                    full = None
                if full is not None:
                    _apply_approute(order, full)
                if order.status != "BOUGHT":
                    order.status = "AWAITING"  # повторим на следующем опросе
    finally:
        await api.aclose()

    if order.status == "FAILED":
        await _fail(bot, session, seller, client, order, FazerError(order.error or "AppRoute отменил заказ"))
        return
    if order.status == "BOUGHT":
        if order.error and order.error.startswith("частично"):
            await _tell_admin(bot, session, order, f"{order.error}. Выданные коды отправлены покупателю.")
        await _deliver(bot, session, seller, client, order, deal)


def _apply_approute(order: GiftcardOrder, result: dict[str, Any]) -> tuple[list[str], bool]:
    """Ответ AppRoute (покупка или строка из GET /orders) → состояние заказа.
    Возвращает (коды, скрыты ли коды)."""
    tg = order.seller_tg_id
    order.updated_at = _now()
    oid = result.get("orderId") or result.get("transactionUUID") or result.get("transactionUuid")
    if oid:
        order.provider_order_id = str(oid)[:64]
    status = norm_status(result.get("status"))
    price = result.get("price", result.get("amount"))
    try:
        if price is not None and float(price) >= 0:
            order.cost_usd, order.cost_exact = float(price), True
    except (TypeError, ValueError):
        pass
    if status == "CANCELLED":
        order.status = "FAILED"
        order.error = "AppRoute отменил заказ"
        log.warning("%s GIFTCARD API_ERROR AppRoute заказ %s отменён", tag(tg), order.provider_order_id)
        return [], False
    vouchers = (result.get("result") or {}).get("vouchers") if isinstance(result.get("result"), dict) else None
    codes, masked = voucher_codes(vouchers if vouchers is not None else result.get("vouchers"))
    if codes and not masked:
        order.codes_enc = _cipher().encrypt("\n\n".join(codes))
        order.status = "BOUGHT"
        order.error = None
        if status == "PARTIALLY_COMPLETED" and len(codes) < (order.quantity or 1):
            order.error = f"частично: кодов {len(codes)} из {order.quantity}"
        log.info("%s GIFTCARD API_SUCCESS AppRoute заказ %s: получено кодов %s", tag(tg), order.provider_order_id,
                 len(codes))
    else:
        order.status = "AWAITING"
        log.info("%s GIFTCARD API_SUCCESS AppRoute заказ %s, коды %s (статус %s)", tag(tg), order.provider_order_id,
                 "скрыты — запрошу полные" if masked else "ещё не готовы", status)
    return codes, masked


def _apply_order(order: GiftcardOrder, result: dict[str, Any]) -> bool:
    """Применяет ответ поставщика к заказу. True — поставщик отменил заказ."""
    tg = order.seller_tg_id
    order.provider_order_id = provider_order_id(result) or order.provider_order_id
    order.updated_at = _now()
    if order_failed(result):
        return True
    try:
        codes = extract_codes(result)
    except UnknownCodeFormat as e:
        order.status = "NEEDS_CHECK"
        order.error = f"неизвестный формат кода: {e}"
        log.warning("%s GIFTCARD API_ERROR заказ %s: %s", tag(tg), order.provider_order_id, order.error)
        return False
    cost = extract_cost(result)
    if cost is not None:
        order.cost_usd, order.cost_exact = cost, True
    if codes:
        order.codes_enc = _cipher().encrypt("\n\n".join(codes))
        order.status = "BOUGHT"
        order.error = None
        log.info("%s GIFTCARD API_SUCCESS заказ %s: получено кодов %s", tag(tg), order.provider_order_id, len(codes))
    else:
        order.status = "AWAITING"
        log.info("%s GIFTCARD API_SUCCESS заказ %s создан, коды ещё не готовы (статус %s)",
                 tag(tg), order.provider_order_id, result.get("status"))
    return False


async def _deliver(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, order: GiftcardOrder, deal: Deal | None
) -> None:
    tg = order.seller_tg_id
    await session.commit()  # коды сохранены до отправки: при сбое повторим только отправку
    codes = _cipher().decrypt(order.codes_enc or "")
    text = ft.render(DONE_TEXT, seller, deal, buyer=order.buyer or "").replace("{Код}", codes)
    try:
        if not order.chat_id:
            raise PlayerokError("у сделки нет чата")
        await client.send_message(order.chat_id, text)
    except PlayerokError as e:
        order.error = f"не отправлено в чат: {e}"[:500]
        log.warning("%s GIFTCARD DELIVERY_FAILED сделка %s: %s — повторю отправку", tag(tg), order.deal_id, e)
        return
    order.status = "DELIVERED"
    order.error = None
    order.updated_at = _now()
    log.info("%s GIFTCARD DELIVERY_SUCCESS сделка %s, заказ поставщика %s", tag(tg), order.deal_id,
             order.provider_order_id)
    if order.cost_usd is None:
        await _cost_from_catalog(session, order)
    await notify(
        bot, session, tg, "system",
        f"🎁 Gift Card выдана: «{html.escape(order.item_name)}» → {html.escape(order.buyer or 'покупатель')}.",
    )


async def _cost_from_catalog(session: AsyncSession, order: GiftcardOrder) -> None:
    """Цена номинала из каталога × количество — если в ответе заказа суммы не было.
    Уже после выдачи, чтобы не задерживать покупателя; ошибки не важны."""
    try:
        if provider_of(order) == "approute":
            async with await make_approute(session, order.seller_tg_id) as ar:
                product = await ar.service(order.category_id)
            item = next((i for i in product.get("items") or [] if str(i.get("id")) == order.card_id), None)
            if item is not None:
                order.cost_usd = float(item.get("price")) * (order.quantity or 1)
                order.cost_exact = False
            return
        async with make_client(await get_api_key(session, order.seller_tg_id)) as api:
            data = await api.offers(order.category_id)
        offer = next((o for o in data.get("offers") or [] if str(o.get("card_id")) == order.card_id), None)
        if offer is not None:
            order.cost_usd = float(offer.get("price_usd")) * (order.quantity or 1)
            order.cost_exact = False
    except (FazerError, TypeError, ValueError):
        pass


async def _fail(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, order: GiftcardOrder, e: FazerError
) -> None:
    order.status = "FAILED"
    order.error = str(e)[:500]
    order.updated_at = _now()
    kind = "ключ API" if isinstance(e, FazerAuthError) else "поставщик отказал"
    log.warning("%s GIFTCARD API_ERROR сделка %s: %s — %s", tag(order.seller_tg_id), order.deal_id, kind, e)
    if not order.buyer_told and order.chat_id:
        try:
            await client.send_message(order.chat_id, FAIL_TEXT)
            order.buyer_told = True
        except PlayerokError:
            pass
    await _tell_admin(bot, session, order, f"{kind}: {e}. Заказ не выдан и не подтверждён — выдай вручную "
                                           "или нажми «Повторить» в /giftcard → «Заказы».")


async def _tell_admin(bot: Bot, session: AsyncSession, order: GiftcardOrder, text: str) -> None:
    if order.admin_told:
        return
    order.admin_told = True
    await notify(
        bot, session, order.seller_tg_id, "problem",
        f"🎁 Gift Card, заказ «{html.escape(order.item_name)}» ({html.escape(order.buyer or '—')}): "
        f"{html.escape(text)}",
    )


async def retry(session: AsyncSession, order: GiftcardOrder) -> str:
    """Ручной повтор из /giftcard. FAILED — поставщик точно не продал, можно новый ключ;
    UNKNOWN — повтор с ТЕМ ЖЕ ключом (вернёт исходный заказ, если он был)."""
    if order.status == "FAILED":
        order.idem_key = f"{order.idem_key.split('#')[0]}#r{order.attempts + 1}"
        order.status = "PROCESSING"
    elif order.status == "UNKNOWN":
        order.attempts = 0
    elif order.status == "NEEDS_CHECK" and order.codes_enc:
        order.status = "BOUGHT"
    else:
        return "Этот заказ повторять не нужно."
    order.admin_told = False
    order.error = None
    order.updated_at = _now()
    await session.commit()
    return "Повторю на ближайшем опросе (≤30 с)."


async def stats(session: AsyncSession, tg: int) -> dict[str, int]:
    rows = await session.execute(
        select(GiftcardOrder.status, func.count(GiftcardOrder.id))
        .where(GiftcardOrder.seller_tg_id == tg)
        .group_by(GiftcardOrder.status)
    )
    return {s: n for s, n in rows.all()}
