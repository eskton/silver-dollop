"""Вход в Playerok по почте: e-mail → код из письма → токен сессии в базе."""

import html
import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from ..crypto import TokenCipher
from ..db import SessionFactory
from ..keyboards import BTN_CANCEL, BTN_LOGIN, cancel_kb, main_menu
from ..playerok import PlayerokClient, PlayerokError
from ..services.poller import sync_seller
from ..services.sellers import get_or_create_seller

router = Router(name="auth")
log = logging.getLogger(__name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class Login(StatesGroup):
    email = State()
    code = State()


# Клиент, через который запрошен код, живёт до конца входа: токен приходит
# в cookie, и сайт может привязать код к тому же HTTP-клиенту.
_pending: dict[int, PlayerokClient] = {}


async def _drop_pending(tg_id: int) -> None:
    client = _pending.pop(tg_id, None)
    if client is not None:
        await client.aclose()


@router.message(StateFilter(Login), F.text == BTN_CANCEL)
@router.message(StateFilter(Login), Command("cancel"))
async def cancel_login(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    await _drop_pending(message.from_user.id)
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Вход отменён.", reply_markup=main_menu(seller.is_connected))


@router.message(F.text == BTN_LOGIN)
@router.message(Command("login"))
async def start_login(message: Message, state: FSMContext) -> None:
    await _drop_pending(message.from_user.id)
    await state.set_state(Login.email)
    await message.answer(
        "Введи e-mail, на который зарегистрирован аккаунт Playerok:",
        reply_markup=cancel_kb(),
    )


@router.message(Login.email, F.text)
async def got_email(message: Message, state: FSMContext) -> None:
    email = message.text.strip().lower()
    if not EMAIL_RE.match(email):
        await message.answer("Это не похоже на e-mail. Попробуй ещё раз или нажми «Отмена».")
        return

    client = PlayerokClient()
    try:
        await client.request_email_code(email)
    except PlayerokError as e:
        await client.aclose()
        await message.answer(
            f"Playerok не принял почту: {html.escape(str(e))}\n"
            "Попробуй ещё раз или нажми «Отмена»."
        )
        return

    _pending[message.from_user.id] = client
    await state.update_data(email=email)
    await state.set_state(Login.code)
    await message.answer("Отправил код на почту. Введи его сюда:")


@router.message(Login.code, F.text)
async def got_code(
    message: Message,
    state: FSMContext,
    bot: Bot,
    sessions: SessionFactory,
    cipher: TokenCipher,
) -> None:
    code = re.sub(r"\D", "", message.text)
    if not code:
        await message.answer("Код состоит из цифр. Введи его ещё раз или нажми «Отмена».")
        return

    client = _pending.get(message.from_user.id)
    if client is None:
        await state.clear()
        await message.answer(
            "Сессия входа потерялась (бот перезапускался). Начни заново: /login",
            reply_markup=main_menu(False),
        )
        return

    email = (await state.get_data())["email"]
    try:
        viewer = await client.confirm_email_code(email, code)
    except PlayerokError as e:
        await message.answer(
            f"Код не подошёл: {html.escape(str(e))}\nВведи ещё раз или нажми «Отмена»."
        )
        return

    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
        seller.email = email
        seller.playerok_id = viewer.id
        seller.playerok_username = viewer.username
        seller.token_enc = cipher.encrypt(client.token)
        await session.commit()
    await state.clear()

    # Помечаем текущие заказы и сообщения увиденными, чтобы не слать старое.
    try:
        await sync_seller(bot, sessions, cipher, seller, notify=False, client=client)
    except PlayerokError as e:
        log.warning("Не удалось снять базовый срез для %s: %s", seller.tg_id, e)
    finally:
        await _drop_pending(message.from_user.id)

    await message.answer(
        f"Готово! Вошёл как <b>{html.escape(viewer.username)}</b>.\n"
        "Теперь буду присылать уведомления о заказах и сообщениях покупателей.",
        reply_markup=main_menu(True),
    )
