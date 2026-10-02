from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..db import SessionFactory
from ..keyboards import BTN_HELP, main_menu
from ..services.sellers import get_or_create_seller

router = Router(name="start")

HELP_TEXT = (
    "Я помощник продавца на Playerok. Подключи свой аккаунт, и я буду:\n"
    "• присылать уведомления о заказах, сообщениях, отзывах и проблемах;\n"
    "• здороваться с покупателями, выдавать товар и подтверждать заказы;\n"
    "• отвечать по ключевым словам, напоминать о подтверждении и отзыве;\n"
    "• поднимать и перевыставлять лоты.\n\n"
    "Команды:\n"
    "/login — войти в Playerok по почте и коду\n"
    "/settings — автоматизация: тексты, автовыдача, автоответчик\n"
    "/stats — аналитика: продажи, выручка, топ лотов, отзывы\n"
    "/account — состояние аккаунта, выход\n"
    "/notify — что присылать\n"
    "/cancel — отменить текущее действие"
)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    if seller.is_connected:
        text = f"С возвращением! Аккаунт <b>{seller.playerok_username}</b> подключён."
    else:
        text = "Привет! " + HELP_TEXT + "\n\nНачни с кнопки «Войти в Playerok»."
    await message.answer(text, reply_markup=main_menu(seller.is_connected))


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, sessions: SessionFactory) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(HELP_TEXT, reply_markup=main_menu(seller.is_connected))
