"""Ответ покупателю из Telegram: кнопка под уведомлением → текст → Playerok."""

import html

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from ..crypto import TokenCipher
from ..db import SessionFactory
from ..keyboards import BTN_CANCEL, cancel_kb, main_menu
from ..playerok import AuthRequired, PlayerokClient, PlayerokError
from ..services.sellers import disconnect_seller, get_or_create_seller

router = Router(name="chat")


class Reply(StatesGroup):
    text = State()


@router.callback_query(F.data.startswith("reply:"))
async def ask_reply(cb: CallbackQuery, state: FSMContext) -> None:
    chat_id = cb.data.split(":", 1)[1]
    await state.set_state(Reply.text)
    await state.update_data(chat_id=chat_id)
    await cb.answer()
    await cb.message.answer("Напиши ответ покупателю:", reply_markup=cancel_kb())


@router.message(StateFilter(Reply), F.text == BTN_CANCEL)
@router.message(StateFilter(Reply), Command("cancel"))
async def cancel_reply(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))


@router.message(Reply.text, F.text)
async def send_reply(
    message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher
) -> None:
    chat_id = (await state.get_data())["chat_id"]
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    if not seller.is_connected:
        await state.clear()
        await message.answer("Аккаунт Playerok не подключён.", reply_markup=main_menu(False))
        return

    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        try:
            await client.send_message(chat_id, message.text)
        except AuthRequired:
            await state.clear()
            await disconnect_seller(sessions, seller.tg_id)
            await message.answer(
                "⚠️ Сессия истекла, сообщение не отправлено. Войди заново.",
                reply_markup=main_menu(False),
            )
            return
        except PlayerokError as e:
            await message.answer(
                f"Не удалось отправить: {html.escape(str(e))}\nПопробуй ещё раз или нажми «Отмена»."
            )
            return

    await state.clear()
    await message.answer("✅ Отправлено.", reply_markup=main_menu(True))
