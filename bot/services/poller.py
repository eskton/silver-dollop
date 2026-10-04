"""Фоновый опрос Playerok: новые заказы и сообщения → уведомления в Telegram."""

import asyncio
import html
import logging

from aiogram import Bot
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from ..config import Settings
from ..crypto import TokenCipher
from ..db import SeenEvent, Seller, SessionFactory
from ..keyboards import deal_kb, main_menu
from ..logs import tag
from ..playerok import AuthRequired, ChatPreview, Deal, PlayerokClient, PlayerokError
from ..playerok.client import ACTIVE_SALE_STATUSES
from . import automation, notifications
from .sellers import disconnect_seller

log = logging.getLogger(__name__)

STATUS_RU = {
    "PAID": "оплачен, ждёт выдачи",
    "SENT": "выдан, ждёт подтверждения",
    "CONFIRMED": "подтверждён",
    "COMPLETED": "завершён",
}


async def run_poller(
    bot: Bot, sessions: SessionFactory, cipher: TokenCipher, settings: Settings
) -> None:
    log.info("Поллер запущен, интервал %s с", settings.poll_interval)
    while True:
        try:
            await poll_once(bot, sessions, cipher)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка в цикле поллера")
        await asyncio.sleep(settings.poll_interval)


async def poll_once(bot: Bot, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        sellers = list(await session.scalars(select(Seller).where(Seller.token_enc.is_not(None))))

    for seller in sellers:
        try:
            await sync_seller(bot, sessions, cipher, seller)
        except AuthRequired:
            log.info("%s сессия Playerok истекла", tag(seller.tg_id))
            await disconnect_seller(sessions, seller.tg_id)
            await bot.send_message(
                seller.tg_id,
                "⚠️ Сессия Playerok истекла. Нажми «Войти в Playerok», чтобы подключить аккаунт заново.",
                reply_markup=main_menu(False),
            )
        except PlayerokError as e:
            log.warning("%s ошибка Playerok: %s", tag(seller.tg_id), e)
        except Exception:
            log.exception("%s сбой обработки продавца", tag(seller.tg_id))


async def sync_seller(
    bot: Bot,
    sessions: SessionFactory,
    cipher: TokenCipher,
    seller: Seller,
    *,
    notify: bool = True,
    client: PlayerokClient | None = None,
) -> None:
    """Забирает свежие события продавца. С notify=False только помечает их
    увиденными: так после входа старые заказы не сыплются уведомлениями."""
    if not seller.token_enc or not seller.playerok_id:
        return
    own_client = client is None
    if client is None:
        client = PlayerokClient(cipher.decrypt(seller.token_enc))
    try:
        # При первом проходе после входа берём больше истории — для аналитики.
        deals = await client.my_sales(seller.playerok_id, limit=30 if notify else 100)
        for deal in deals:
            if not deal.id or deal.status not in ACTIVE_SALE_STATUSES:
                continue
            is_new = await _mark_seen(sessions, seller.tg_id, "deal", deal.id)
            if is_new and notify:
                number = await _daily_order_number(sessions, seller.tg_id)
                account = seller.playerok_username or "—"
                async with sessions() as session:
                    await notifications.notify(
                        bot, session, seller.tg_id, "deal",
                        _format_deal(deal, account, number),
                        reply_markup=deal_kb(deal.chat_id, deal.item_id),
                    )
        await automation.process_deals(bot, sessions, seller, client, deals, act=notify)

        chats = await client.chats(seller.playerok_id)
        for chat in chats:
            if chat.unread <= 0 or not chat.last_message_id:
                continue
            if chat.last_author_id == seller.playerok_id:
                continue
            is_new = await _mark_seen(sessions, seller.tg_id, "message", chat.last_message_id)
            if is_new and notify:
                async with sessions() as session:
                    await notifications.notify(
                        bot, session, seller.tg_id, "message", _format_message(chat),
                        reply_markup=deal_kb(chat.id),
                    )
        await automation.process_chats(
            sessions, seller, client, chats, act=notify, bot=bot, cipher=cipher
        )

        if notify:
            await automation.process_items(bot, sessions, seller, client)
    finally:
        if own_client:
            await client.aclose()


async def _mark_seen(sessions: SessionFactory, tg_id: int, kind: str, external_id: str) -> bool:
    """True, если событие ещё не встречалось (и теперь записано)."""
    async with sessions() as session:
        session.add(SeenEvent(seller_tg_id=tg_id, kind=kind, external_id=external_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return False
        return True


async def _daily_order_number(sessions: SessionFactory, tg_id: int) -> int:
    """Какой это по счёту заказ за сегодня (текущий уже записан в SeenEvent)."""
    since = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with sessions() as session:
        n = await session.scalar(
            select(func.count(SeenEvent.id)).where(
                SeenEvent.seller_tg_id == tg_id,
                SeenEvent.kind == "deal",
                SeenEvent.created_at >= since,
            )
        )
    return int(n or 1)


def _link(url: str, text: str) -> str:
    return f'<a href="{url}">{text}</a>'


def _format_deal(deal: Deal, account: str, number: int) -> str:
    price = f"{deal.price:g} ₽" if isinstance(deal.price, (int, float)) else "—"
    lines = [
        f"💰 <b>Заказ №{number} за сегодня для {html.escape(account)}</b>",
        "",
        f"<b>Название товара:</b> {_link(deal.item_url, html.escape(deal.item_name))}",
        f"<b>Цена:</b> {price}",
        f"<b>Заказ:</b> {_link(deal.deal_url, 'Открыть заказ')}",
    ]
    if deal.chat_id:
        lines.append(f"<b>Ссылка на чат:</b> {_link(deal.chat_url, 'Открыть чат')}")
    lines.append(f"<b>Аккаунт покупателя:</b> {html.escape(deal.buyer_username)}")
    return "\n".join(lines)


def _format_message(chat: ChatPreview) -> str:
    text = chat.last_text.strip() or "(без текста)"
    if len(text) > 1500:
        text = text[:1500] + "…"
    chat_url = f"https://playerok.com/chats/{chat.id}"
    return (
        f"✉️ <b>Новое сообщение от {html.escape(chat.last_author_username)}</b>\n\n"
        f"{html.escape(text)}\n\n"
        f"<b>Ссылка на чат:</b> {_link(chat_url, 'Открыть чат')}"
    )
