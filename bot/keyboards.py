from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

BTN_ACCOUNT = "👤 Аккаунт"
BTN_LOGIN = "🔑 Войти в Playerok"
BTN_NOTIFY = "🔔 Уведомления"
BTN_HELP = "ℹ️ Помощь"
BTN_CANCEL = "❌ Отмена"
BTN_SETTINGS = "⚙️ Автоматизация"
BTN_STATS = "📊 Аналитика"
BTN_PROFILE = "👤 Профиль"


def is_cancel(text: str | None) -> bool:
    """Кнопка «Отмена» или то же слово, набранное вручную."""
    return (text or "").replace("❌", "").strip().lower() in ("отмена", "cancel")


def main_menu(connected: bool) -> ReplyKeyboardMarkup:
    if connected:
        rows = [
            [KeyboardButton(text=BTN_SETTINGS), KeyboardButton(text=BTN_STATS)],
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_HELP)],
        ]
    else:
        rows = [[KeyboardButton(text=BTN_LOGIN)], [KeyboardButton(text=BTN_HELP)]]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_CANCEL)]], resize_keyboard=True)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def deal_kb(chat_id: str | None, item_id: str | None = None) -> InlineKeyboardMarkup | None:
    rows = []
    if chat_id:
        rows.append([InlineKeyboardButton(text="💬 Ответить покупателю", callback_data=f"reply:{chat_id}")])
    if item_id and len(f"rl:pub:{item_id}".encode()) <= 64:
        rows.append([InlineKeyboardButton(text="🔄 Выставить заново", callback_data=f"rl:pub:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
