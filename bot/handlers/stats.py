from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..db import SessionFactory
from ..keyboards import BTN_STATS, main_menu
from ..services.analytics import build_report
from ..services.sellers import get_or_create_seller

router = Router(name="stats")

REFRESH_KB = InlineKeyboardMarkup(
    inline_keyboard=[[InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:refresh")]]
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
