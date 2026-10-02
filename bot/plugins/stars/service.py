"""Выдача Telegram Stars через Fragment с оплатой USDT (TON).

Поток: оплаченный лот со звёздами → бот просит @username в чате сделки →
проверяет его на Fragment → покупает → пишет покупателю и отмечает заказ
выданным. Все состояния в StarsOrder.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...crypto import TokenCipher
from ...db import Seller, StarsOrder, StarsRule
from ...playerok import Deal, PlayerokClient, PlayerokError
from ...services import features as ft
from ...services.notifications import notify
from ..access import has_access
from .fragment import FragmentAuthError, FragmentClient, FragmentError, RecipientNotFound, parse_cookies
from .ton import TonWallet, TonWalletError

log = logging.getLogger(__name__)

PLUGIN_KEY = "stars"

USERNAME_RE = re.compile(r"@?([A-Za-z][A-Za-z0-9_]{3,31})")
STARS_IN_NAME_RE = re.compile(r"(\d[\d\s]{0,6}\d|\d)\s*(?:⭐|★|звезд|звёзд|stars?)", re.I)
MAX_ATTEMPTS = 3
MIN_STARS, MAX_STARS = 50, 1_000_000


def _now() -> datetime:
    return datetime.utcnow()


async def _say(client: PlayerokClient, chat_id: str | None, text: str) -> bool:
    if not chat_id or not text.strip():
        return False
    try:
        await client.send_message(chat_id, text)
        return True
    except PlayerokError as e:
        log.warning("Звёзды: не удалось написать в чат %s: %s", chat_id, e)
        return False


# ----- настройки продавца -----


async def get_cookies(session: AsyncSession, seller: Seller, cipher: TokenCipher) -> dict[str, str]:
    enc = await ft.get_setting(session, seller.tg_id, "stars_cookies_enc", "")
    return parse_cookies(cipher.decrypt(enc)) if enc else {}


async def get_wallet(session: AsyncSession, seller: Seller, cipher: TokenCipher) -> TonWallet | None:
    enc = await ft.get_setting(session, seller.tg_id, "stars_seed_enc", "")
    if not enc:
        return None
    version = await ft.get_setting(session, seller.tg_id, "stars_wallet_version", "v5r1")
    return TonWallet(cipher.decrypt(enc), version=version)


async def stars_for_lot(session: AsyncSession, tg_id: int, item_name: str) -> int | None:
    """Сколько звёзд выдавать за лот: сначала правила, потом число из названия."""
    name = item_name.lower()
    rules = await session.scalars(select(StarsRule).where(StarsRule.seller_tg_id == tg_id))
    for rule in rules:
        if rule.pattern.lower() in name:
            return rule.stars
    if await ft.get_flag(session, tg_id, "stars_autoparse", True):
        m = STARS_IN_NAME_RE.search(item_name)
        if m:
            qty = int(re.sub(r"\s", "", m.group(1)))
            if MIN_STARS <= qty <= MAX_STARS:
                return qty
    return None


# ----- события -----


async def on_paid(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, deal: Deal
) -> bool:
    """Новый оплаченный заказ. True — это заказ звёзд, бот взял его в работу."""
    tg = seller.tg_id
    if not await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["stars"]):
        return False
    if not await has_access(session, tg, PLUGIN_KEY):
        return False
    qty = await stars_for_lot(session, tg, deal.item_name)
    if qty is None:
        return False
    existing = await session.scalar(
        select(StarsOrder).where(StarsOrder.seller_tg_id == tg, StarsOrder.deal_id == deal.id)
    )
    if existing is not None:
        return True
    order = StarsOrder(
        seller_tg_id=tg,
        deal_id=deal.id,
        chat_id=deal.chat_id,
        item_name=deal.item_name,
        buyer=deal.buyer_username,
        stars=qty,
        status="awaiting_username",
        updated_at=_now(),
    )
    session.add(order)
    text = ft.render(await ft.get_template(session, tg, "stars_ask_username"), seller, deal)
    await _say(client, deal.chat_id, text.replace("{Звёзды}", str(qty)))
    await notify(
        bot, session, tg, "system",
        f"⭐ Заказ на {qty} звёзд от {html.escape(deal.buyer_username)} («{html.escape(deal.item_name)}»). "
        "Жду @username покупателя.",
    )
    return True


async def on_buyer_message(
    bot: Bot,
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    cipher: TokenCipher,
    chat_id: str,
    text: str,
) -> bool:
    """Сообщение покупателя в чате, где ждём @username. True — сообщение наше."""
    tg = seller.tg_id
    order = await session.scalar(
        select(StarsOrder).where(
            StarsOrder.seller_tg_id == tg,
            StarsOrder.chat_id == chat_id,
            StarsOrder.status == "awaiting_username",
        )
    )
    if order is None:
        return False
    m = USERNAME_RE.search(text or "")
    if not m or m.group(1).lower() in ("http", "https", "привет", "здравствуйте"):
        await _say(client, chat_id, await ft.get_template(session, tg, "stars_bad_username"))
        return True
    order.username = m.group(1)
    order.status = "processing"
    order.updated_at = _now()
    await session.commit()
    await fulfill(bot, session, seller, client, cipher, order)
    return True


async def fulfill(
    bot: Bot,
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    cipher: TokenCipher,
    order: StarsOrder,
) -> None:
    tg = seller.tg_id
    order.attempts = (order.attempts or 0) + 1
    try:
        await _buy(session, seller, cipher, order)
    except FragmentAuthError as e:
        await _fail(bot, session, seller, client, order, f"сессия Fragment: {e}", ask_again=False)
        return
    except RecipientNotFound as e:
        if order.attempts < MAX_ATTEMPTS:
            # Неверный ник — спросим ещё раз
            order.status = "awaiting_username"
            order.error = str(e)
            order.updated_at = _now()
            await _say(client, order.chat_id, await ft.get_template(session, tg, "stars_bad_username"))
            return
        await _fail(bot, session, seller, client, order, str(e), ask_again=False)
        return
    except (FragmentError, TonWalletError) as e:
        await _fail(bot, session, seller, client, order, str(e), ask_again=False)
        return
    except Exception as e:  # noqa: BLE001
        log.exception("Звёзды: заказ %s", order.deal_id)
        await _fail(bot, session, seller, client, order, f"{e.__class__.__name__}: {e}", ask_again=False)
        return

    order.status = "done"
    order.error = None
    order.updated_at = _now()
    done_text = await ft.get_template(session, tg, "stars_done")
    done_text = done_text.replace("{Звёзды}", str(order.stars)).replace("{Юзернейм}", f"@{order.username}")
    await _say(client, order.chat_id, ft.render(done_text, seller, buyer=order.buyer or ""))
    if await ft.get_flag(session, tg, "stars_autoconfirm", True):
        try:
            await client.confirm_deal(order.deal_id)
        except PlayerokError as e:
            log.warning("Звёзды: не удалось подтвердить сделку %s: %s", order.deal_id, e)
    cost = f" за {order.cost_usdt:.2f} USDT" if order.cost_usdt else ""
    await notify(
        bot, session, tg, "system",
        f"✅ Выдал {order.stars} ⭐ на @{html.escape(order.username or '')}{cost}. "
        f"Покупатель: {html.escape(order.buyer or '')}.",
    )
    await _check_balance(bot, session, seller, cipher)


async def _buy(session: AsyncSession, seller: Seller, cipher: TokenCipher, order: StarsOrder) -> None:
    cookies = await get_cookies(session, seller, cipher)
    if not cookies:
        raise FragmentAuthError("cookies Fragment не заданы")
    wallet = await get_wallet(session, seller, cipher)
    if wallet is None:
        raise TonWalletError("кошелёк не подключён")
    try:
        async with FragmentClient(cookies) as fragment:
            recipient = await fragment.search_recipient(order.username or "", order.stars)
            order.recipient_name = recipient.name
            invoice = await fragment.create_invoice(recipient, order.stars)
            order.fragment_req_id = invoice.req_id
            order.cost_usdt = _parse_amount(invoice.amount_text)
            await session.commit()
            order.tx_hash = await wallet.send_fragment_messages(invoice.messages)
            await session.commit()
            try:
                await fragment.check_state(invoice.req_id)
            except FragmentError as e:
                log.info("Звёзды: check_state %s: %s", invoice.req_id, e)
    finally:
        await wallet.aclose()


def _parse_amount(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


async def _fail(
    bot: Bot,
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    order: StarsOrder,
    error: str,
    *,
    ask_again: bool,
) -> None:
    order.status = "failed"
    order.error = error[:1000]
    order.updated_at = _now()
    await session.commit()
    await _say(client, order.chat_id, await ft.get_template(session, seller.tg_id, "stars_fail"))
    await notify(
        bot, session, seller.tg_id, "problem",
        f"❌ Не удалось выдать {order.stars} ⭐ на @{html.escape(order.username or '?')} "
        f"(покупатель {html.escape(order.buyer or '')}):\n<code>{html.escape(error[:500])}</code>\n"
        "Выдай вручную или нажми «Повторить» в настройках звёзд.",
    )


async def _check_balance(bot: Bot, session: AsyncSession, seller: Seller, cipher: TokenCipher) -> None:
    wallet = await get_wallet(session, seller, cipher)
    if wallet is None:
        return
    try:
        b = await wallet.balances()
    except TonWalletError:
        return
    finally:
        await wallet.aclose()
    min_usdt = await ft.get_param(session, seller.tg_id, "stars_min_usdt")
    if b.usdt < min_usdt or b.ton < 0.2:
        await notify(
            bot, session, seller.tg_id, "out_of_stock",
            f"⚠️ Кошелёк для звёзд: {b.usdt:.2f} USDT, {b.ton:.3f} TON. Пополни, иначе выдача остановится.",
        )


async def retry(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, cipher: TokenCipher, order: StarsOrder
) -> None:
    order.status = "processing"
    order.updated_at = _now()
    await session.commit()
    await fulfill(bot, session, seller, client, cipher, order)
