"""Импорт всей истории продаж продавца с Playerok в базу (для аналитики)."""

from __future__ import annotations

import logging

from aiogram import Bot

from ..crypto import TokenCipher
from ..db import Seller, SessionFactory
from ..playerok import PlayerokClient, PlayerokError
from . import automation

log = logging.getLogger(__name__)


async def import_history(
    bot: Bot,
    sessions: SessionFactory,
    cipher: TokenCipher,
    seller: Seller,
    client: PlayerokClient | None = None,
) -> int:
    """Выкачивает все продажи и записывает их в базу без каких-либо действий
    и уведомлений. Возвращает число заказов. Ошибки Playerok пробрасывает."""
    if not seller.token_enc or not seller.playerok_id:
        return 0
    own = client is None
    if client is None:
        client = PlayerokClient(cipher.decrypt(seller.token_enc))
    try:
        deals = await client.all_sales(seller.playerok_id)
        await automation.process_deals(bot, sessions, seller, client, deals, act=False)
        log.info("История продавца %s: %d заказов", seller.tg_id, len(deals))
        return len(deals)
    finally:
        if own:
            await client.aclose()


async def import_history_quietly(
    bot: Bot, sessions: SessionFactory, cipher: TokenCipher, seller: Seller
) -> None:
    """То же, но для фона: ошибку пишет в лог и сообщает продавцу."""
    try:
        count = await import_history(bot, sessions, cipher, seller)
    except PlayerokError as e:
        log.warning("Импорт истории %s: %s", seller.tg_id, e)
        await bot.send_message(
            seller.tg_id,
            "⚠️ Не удалось загрузить историю заказов с Playerok: "
            f"<code>{e}</code>\nВ аналитике есть кнопка «Загрузить историю», чтобы повторить.",
        )
        return
    except Exception:
        log.exception("Импорт истории %s", seller.tg_id)
        return
    await bot.send_message(seller.tg_id, f"📊 Загрузил историю заказов: {count} шт. Аналитика готова.")
