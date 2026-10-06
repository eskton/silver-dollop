"""Команды администратора: выдача и отзыв доступа к платным плагинам."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message
from sqlalchemy import select

from ..db import Seller, SessionFactory
from ..plugins import PAID_PLUGINS
from ..plugins import access as ac

router = Router(name="admin")
router.message.filter(F.from_user.id.func(ac.is_admin))

HELP = (
    "🛠 <b>Админ</b>\n\n"
    "/grant &lt;id или @username&gt; &lt;плагин&gt; [дней] — выдать доступ (без дней — навсегда)\n"
    "/revoke &lt;id или @username&gt; &lt;плагин&gt; — отозвать\n"
    "/access [плагин] — кому выдан доступ\n"
    "/sellers — продавцы бота\n"
    "/giftcard — 🎁 Gift Card через FazerCards (только для админа)\n\n"
    "Плагины: " + ", ".join(f"<code>{k}</code> ({v})" for k, v in PAID_PLUGINS.items())
)


async def _resolve(session, who: str) -> Seller | None:
    who = who.strip()
    if who.lstrip("-").isdigit():
        return await session.get(Seller, int(who))
    return await session.scalar(select(Seller).where(Seller.tg_username == who.lstrip("@")))


def _plugin(name: str) -> str | None:
    name = name.strip().lower()
    return name if name in PAID_PLUGINS else None


@router.message(Command("admin"))
async def admin_help(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("grant"))
async def grant_cmd(message: Message, command: CommandObject, sessions: SessionFactory) -> None:
    args = (command.args or "").split()
    if len(args) < 2:
        await message.answer("Формат: /grant &lt;id или @username&gt; &lt;плагин&gt; [дней]")
        return
    plugin = _plugin(args[1])
    if plugin is None:
        await message.answer(f"Нет такого плагина. Есть: {', '.join(PAID_PLUGINS)}")
        return
    days = int(args[2]) if len(args) > 2 and args[2].isdigit() else None
    async with sessions() as session:
        seller = await _resolve(session, args[0])
        if seller is None:
            await message.answer(
                "Не нашёл такого пользователя. Он должен хотя бы раз нажать /start в боте; "
                "по @username ищу только если он есть в Telegram-профиле."
            )
            return
        row = await ac.grant(session, seller.tg_id, plugin, days, message.from_user.id)
    until = f"до {row.expires_at:%d.%m.%Y}" if row.expires_at else "бессрочно"
    await message.answer(
        f"✅ {html.escape(seller.tg_username or str(seller.tg_id))}: {PAID_PLUGINS[plugin]} — {until}."
    )
    try:
        await message.bot.send_message(
            seller.tg_id, f"🎉 Вам открыт доступ к плагину {PAID_PLUGINS[plugin]} ({until}). "
            "Он в меню «⚙️ Автоматизация»."
        )
    except Exception:  # noqa: BLE001 — пользователь мог заблокировать бота
        pass


@router.message(Command("revoke"))
async def revoke_cmd(message: Message, command: CommandObject, sessions: SessionFactory) -> None:
    args = (command.args or "").split()
    if len(args) < 2 or _plugin(args[1]) is None:
        await message.answer("Формат: /revoke &lt;id или @username&gt; &lt;плагин&gt;")
        return
    async with sessions() as session:
        seller = await _resolve(session, args[0])
        ok = seller is not None and await ac.revoke(session, seller.tg_id, _plugin(args[1]))
    await message.answer("✅ Доступ отозван." if ok else "У этого пользователя доступа и не было.")


@router.message(Command("access"))
async def access_cmd(message: Message, command: CommandObject, sessions: SessionFactory) -> None:
    plugin = _plugin(command.args or "") or next(iter(PAID_PLUGINS))
    async with sessions() as session:
        rows = await ac.list_access(session, plugin)
        lines = [f"<b>{PAID_PLUGINS[plugin]}</b> — доступ есть у:"]
        for r in rows:
            seller = await session.get(Seller, r.seller_tg_id)
            name = html.escape(seller.tg_username) if seller and seller.tg_username else str(r.seller_tg_id)
            until = f"до {r.expires_at:%d.%m.%Y}" if r.expires_at else "бессрочно"
            lines.append(f"• {name} (<code>{r.seller_tg_id}</code>) — {until}")
    if len(lines) == 1:
        lines.append("<i>никого, кроме админов</i>")
    lines.append(f"\nАдмины: {', '.join(str(a) for a in sorted(ac.admin_ids())) or 'не заданы (ADMIN_IDS)'}")
    await message.answer("\n".join(lines))


@router.message(Command("sellers"))
async def sellers_cmd(message: Message, sessions: SessionFactory) -> None:
    async with sessions() as session:
        sellers = list(await session.scalars(select(Seller).order_by(Seller.created_at.desc()).limit(50)))
    lines = [f"Продавцов: {len(sellers)}"]
    for s in sellers:
        state = "🟢" if s.is_connected else "⚪"
        name = html.escape(s.tg_username or "—")
        lines.append(f"{state} {name} (<code>{s.tg_id}</code>) · {html.escape(s.playerok_username or '—')}")
    await message.answer("\n".join(lines))
