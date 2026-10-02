"""Доступ к платным плагинам: выдаёт администратор бота, навсегда или на срок."""

from __future__ import annotations

import html
import os
from datetime import datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import PluginAccess
from ..services import features as ft


def admin_ids() -> frozenset[int]:
    raw = os.getenv("ADMIN_IDS", "").replace(",", " ").split()
    return frozenset(int(x) for x in raw if x.strip().lstrip("-").isdigit())


def is_admin(tg_id: int) -> bool:
    return tg_id in admin_ids()


def owner_contact() -> str:
    return os.getenv("OWNER_CONTACT", "").strip()


async def get_access(session: AsyncSession, tg_id: int, plugin: str) -> PluginAccess | None:
    return await session.scalar(
        select(PluginAccess).where(PluginAccess.seller_tg_id == tg_id, PluginAccess.plugin == plugin)
    )


async def has_access(session: AsyncSession, tg_id: int, plugin: str) -> bool:
    """Админу можно всё; остальным — если есть запись и срок не вышел."""
    if is_admin(tg_id):
        return True
    row = await get_access(session, tg_id, plugin)
    if row is None:
        return False
    return row.expires_at is None or row.expires_at > datetime.utcnow()


async def grant(
    session: AsyncSession, tg_id: int, plugin: str, days: int | None, granted_by: int
) -> PluginAccess:
    row = await get_access(session, tg_id, plugin)
    expires = datetime.utcnow() + timedelta(days=days) if days else None
    if row is None:
        row = PluginAccess(seller_tg_id=tg_id, plugin=plugin, granted_by=granted_by, expires_at=expires)
        session.add(row)
    else:
        row.expires_at = expires
        row.granted_by = granted_by
    await session.commit()
    return row


async def revoke(session: AsyncSession, tg_id: int, plugin: str) -> bool:
    row = await get_access(session, tg_id, plugin)
    if row is None:
        return False
    await session.delete(row)
    await session.commit()
    return True


async def list_access(session: AsyncSession, plugin: str) -> list[PluginAccess]:
    return list(
        await session.scalars(
            select(PluginAccess).where(PluginAccess.plugin == plugin).order_by(PluginAccess.id)
        )
    )


async def locked_screen(session: AsyncSession, feature: ft.Feature) -> tuple[str, InlineKeyboardMarkup]:
    contact = owner_contact()
    who = f" Напишите {html.escape(contact)}." if contact else " Напишите владельцу бота."
    text = (
        f"<b>{feature.title}</b> 🔒\n\n"
        f"{feature.description}\n\n"
        f"Это платный плагин, доступ к нему выдаёт владелец бота.{who}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="‹ Назад", callback_data="st")]])
    return text, kb
