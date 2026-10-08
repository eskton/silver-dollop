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


def _fits(data: str) -> bool:
    return len(data.encode()) <= 64  # лимит Telegram на callback_data


def deal_kb(
    chat_id: str | None, item_id: str | None = None, deal_id: str | None = None,
    *, actions: bool = True,
) -> InlineKeyboardMarkup | None:
    """Кнопки под уведомлением (как в Easy Sell): [Ответ][Шаблоны][Весь чат] /
    [Подтвердить][Возврат]. Заказ — по deal_id, иначе последний заказ в этом чате.
    actions=False — без «Подтвердить/Возврат» (заказ уже закрыт)."""
    rows = []
    if chat_id and _fits(f"cm:all:{chat_id}") and _fits(f"cm:ts:99999999:{chat_id}"):
        rows.append([
            InlineKeyboardButton(text="✉️ Ответ", callback_data=f"reply:{chat_id}"),
            InlineKeyboardButton(text="📋 Шаблоны", callback_data=f"cm:tpl:{chat_id}"),
            InlineKeyboardButton(text="💬 Весь чат", callback_data=f"cm:all:{chat_id}"),
        ])
    elif chat_id and _fits(f"reply:{chat_id}"):
        rows.append([InlineKeyboardButton(text="✉️ Ответ", callback_data=f"reply:{chat_id}")])
    ref = f"d{deal_id}" if deal_id else (f"c{chat_id}" if chat_id else "")
    if actions and ref and _fits(f"cm:rfy:{ref}"):
        rows.append([
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"cm:ok:{ref}"),
            InlineKeyboardButton(text="↩️ Возврат", callback_data=f"cm:rf:{ref}"),
        ])
    if item_id and _fits(f"rl:pub:{item_id}"):
        rows.append([InlineKeyboardButton(text="🔄 Выставить заново", callback_data=f"rl:pub:{item_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
