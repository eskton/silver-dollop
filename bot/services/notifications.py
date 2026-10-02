"""Уведомления продавцу: для каждого типа события — вкл/выкл и со звуком/без.

Все сообщения бота продавцу о событиях на Playerok идут через `notify`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from . import features as ft

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class NotifyKind:
    key: str
    title: str
    default_sound: bool = True


KINDS: tuple[NotifyKind, ...] = (
    NotifyKind("deal", "Новый заказ"),
    NotifyKind("message", "Новое сообщение"),
    NotifyKind("review", "Новый отзыв"),
    NotifyKind("confirmed", "Заказ подтверждён"),
    NotifyKind("relisted", "Лот восстановлен"),
    NotifyKind("system", "Системные сообщения", default_sound=False),
    NotifyKind("out_of_stock", "Товар закончился"),
    NotifyKind("problem", "Новая проблема"),
    NotifyKind("refund", "Возврат средств"),
)
KIND_BY_KEY = {k.key: k for k in KINDS}


async def get_prefs(session: AsyncSession, tg_id: int, kind: str) -> tuple[bool, bool]:
    """(включено, со звуком)"""
    k = KIND_BY_KEY[kind]
    on = await ft.get_flag(session, tg_id, f"n_{kind}_on", True)
    sound = await ft.get_flag(session, tg_id, f"n_{kind}_sound", k.default_sound)
    return on, sound


async def toggle(session: AsyncSession, tg_id: int, kind: str, what: str) -> None:
    on, sound = await get_prefs(session, tg_id, kind)
    if what == "on":
        await ft.set_setting(session, tg_id, f"n_{kind}_on", "0" if on else "1")
    elif what == "sound":
        await ft.set_setting(session, tg_id, f"n_{kind}_sound", "0" if sound else "1")


async def notify(
    bot: Bot,
    session: AsyncSession,
    tg_id: int,
    kind: str,
    text: str,
    reply_markup=None,
) -> bool:
    on, sound = await get_prefs(session, tg_id, kind)
    if not on:
        return False
    try:
        await bot.send_message(
            tg_id,
            text,
            reply_markup=reply_markup,
            disable_notification=not sound,
            disable_web_page_preview=True,
        )
        return True
    except TelegramAPIError as e:  # продавец заблокировал бота и т.п.
        log.warning("Не удалось отправить уведомление %s продавцу %s: %s", kind, tg_id, e)
        return False
