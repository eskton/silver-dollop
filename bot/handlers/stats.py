from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import html

from ..crypto import TokenCipher
from ..db import SessionFactory
from ..keyboards import BTN_STATS, main_menu
from ..playerok import AuthRequired, PlayerokError
from ..services.analytics import build_report
from ..services.history import import_history
from ..services.sellers import disconnect_seller, get_or_create_seller

router = Router(name="stats")

REFRESH_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:refresh")],
        [InlineKeyboardButton(text="📥 Загрузить историю с Playerok", callback_data="stats:import")],
    ]
)


@router.message(Command("stats"))
@router.message(F.text == BTN_STATS)
async def show_stats(message: Message, sessions: SessionFactory) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
        if not seller.is_connected:
            await message.answer(
                "Сначала подключи аккаунт Playerok: кнопка «Войти в Playerok».",
                reply_markup=main_menu(False),
            )
            return
        report = await build_report(session, seller.tg_id)
    await message.answer(report, reply_markup=REFRESH_KB)


@router.callback_query(F.data == "stats:refresh")
async def refresh_stats(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        report = await build_report(session, cb.from_user.id)
    try:
        await cb.message.edit_text(report, reply_markup=REFRESH_KB)
    except TelegramBadRequest:
        pass  # ничего не изменилось
    await cb.answer("Обновлено")


@router.callback_query(F.data == "stats:import")
async def import_stats(cb: CallbackQuery, bot: Bot, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Загружаю историю…")
    try:
        count = await import_history(bot, sessions, cipher, seller)
    except AuthRequired:
        await disconnect_seller(sessions, seller.tg_id)
        await cb.message.answer("⚠️ Сессия истекла, войди заново.", reply_markup=main_menu(False))
        return
    except PlayerokError as e:
        await cb.message.answer(
            "⚠️ Playerok не отдал историю заказов:\n"
            f"<code>{html.escape(str(e))}</code>\n\n"
            "Пришли этот текст разработчику — по нему видно, что поправить в запросе."
        )
        return
    async with sessions() as session:
        report = await build_report(session, seller.tg_id)
    await cb.message.answer(f"📥 Загружено заказов: {count}.")
    await cb.message.answer(report, reply_markup=REFRESH_KB)
