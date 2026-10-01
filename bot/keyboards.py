from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from .db import Seller

BTN_ACCOUNT = "👤 Аккаунт"
BTN_LOGIN = "🔑 Войти в Playerok"
BTN_NOTIFY = "🔔 Уведомления"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "❌ Отмена"


def main_menu(connected: bool) -> ReplyKeyboardMarkup:
    first = [KeyboardButton(text=BTN_ACCOUNT)] if connected else [KeyboardButton(text=BTN_LOGIN)]
    return ReplyKeyboardMarkup(
        keyboard=[first, [KeyboardButton(text=BTN_NOTIFY), KeyboardButton(text=BTN_HELP)]],
        resize_keyboard=True,
    )


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def account_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Проверить сессию", callback_data="account:check")],
            [InlineKeyboardButton(text="🚪 Выйти из Playerok", callback_data="account:logout")],
        ]
    )


def notify_kb(seller: Seller) -> InlineKeyboardMarkup:
    def mark(flag: bool) -> str:
        return "✅" if flag else "☑️"

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{mark(seller.notify_deals)} Новые заказы",
                    callback_data="notify:deals",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{mark(seller.notify_messages)} Сообщения покупателей",
                    callback_data="notify:messages",
                )
            ],
        ]
    )


def deal_kb(chat_id: str | None) -> InlineKeyboardMarkup | None:
    if not chat_id:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Ответить покупателю", callback_data=f"reply:{chat_id}")]
        ]
    )
