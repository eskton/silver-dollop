import html

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..crypto import TokenCipher
from ..db import SessionFactory
from ..keyboards import BTN_ACCOUNT, BTN_NOTIFY, account_kb, main_menu, notify_kb
from ..playerok import AuthRequired, PlayerokClient, PlayerokError
from ..services.sellers import disconnect_seller, get_or_create_seller

router = Router(name="account")


def _on(flag: bool) -> str:
    return "вкл" if flag else "выкл"


@router.message(Command("account"))
@router.message(F.text == BTN_ACCOUNT)
async def show_account(message: Message, sessions: SessionFactory) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    if not seller.is_connected:
        await message.answer(
            "Аккаунт Playerok не подключён. Нажми «Войти в Playerok».",
            reply_markup=main_menu(False),
        )
        return
    await message.answer(
        f"<b>Playerok:</b> {html.escape(seller.playerok_username or '—')}\n"
        f"<b>Почта:</b> {html.escape(seller.email or '—')}\n"
        f"<b>Уведомления:</b> заказы {_on(seller.notify_deals)}, "
        f"сообщения {_on(seller.notify_messages)}",
        reply_markup=account_kb(),
    )


@router.callback_query(F.data == "account:check")
async def check_session(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        try:
            viewer = await client.viewer()
        except AuthRequired:
            await disconnect_seller(sessions, seller.tg_id)
            await cb.answer()
            await cb.message.answer(
                "⚠️ Сессия истекла. Нажми «Войти в Playerok», чтобы подключить заново.",
                reply_markup=main_menu(False),
            )
            return
        except PlayerokError as e:
            await cb.answer(f"Ошибка Playerok: {e}"[:200], show_alert=True)
            return
    balance = f", баланс {viewer.balance:g} ₽" if isinstance(viewer.balance, (int, float)) else ""
    await cb.answer(f"Сессия активна: {viewer.username}{balance}", show_alert=True)


@router.callback_query(F.data == "account:logout")
async def logout(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await disconnect_seller(sessions, cb.from_user.id)
    await cb.answer()
    await cb.message.answer(
        "Аккаунт Playerok отключён, токен удалён из базы.", reply_markup=main_menu(False)
    )


@router.message(Command("notify"))
@router.message(F.text == BTN_NOTIFY)
async def notify_settings(message: Message, sessions: SessionFactory) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Что присылать в Telegram:", reply_markup=notify_kb(seller))


@router.callback_query(F.data.startswith("notify:"))
async def toggle_notify(cb: CallbackQuery, sessions: SessionFactory) -> None:
    kind = cb.data.split(":", 1)[1]
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
        if kind == "deals":
            seller.notify_deals = not seller.notify_deals
        elif kind == "messages":
            seller.notify_messages = not seller.notify_messages
        else:
            await cb.answer()
            return
        await session.commit()
    await cb.message.edit_reply_markup(reply_markup=notify_kb(seller))
    await cb.answer()
