"""«Точно удалить?» для всех кнопок удаления.

Внешний middleware корневого роутера: нажатие на кнопку удаления (список DANGEROUS) не
выполняется сразу — бот присылает вопрос с кнопками «🗑 Да, удалить» (y!<исходная кнопка>)
и «Отмена» (n!). «Да» передаёт исходную кнопку обычным обработчикам — их менять не нужно.
"""

from __future__ import annotations

import html
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

YES, NO = "y!", "n!"

# кнопки, которые что-то удаляют безвозвратно
DANGEROUS = [re.compile(p) for p in (
    r"^gc:del:\d+$",          # привязка лота Gift Card
    r"^gc:(ar)?keydel$",      # ключ FazerCards / AppRoute
    r"^dp:del:\d+$",          # правило снижения цен
    r"^ar:del:\d+$",          # автоответ
    r"^rl:del:\d+$",          # правило автовыставления
    r"^sr:rdel:\d+$",         # правило звёзд
    r"^f:[a-z_]+:x:\d+$",     # текст для отдельного лота
    r"^ad:clear:",            # очистка свободного запаса автовыдачи
    r"^cm:tdx:",              # шаблон быстрого ответа
)]
LABELS = {"gc:keydel": "ключ FazerCards", "gc:arkeydel": "ключ AppRoute"}


def is_dangerous(data: str) -> bool:
    return any(p.match(data) for p in DANGEROUS)


def _label(cb: CallbackQuery) -> str:
    """Что удаляем — по тексту нажатой кнопки («🗑 1$ STEAM…» → «1$ STEAM…»)."""
    if cb.data in LABELS:
        return LABELS[cb.data]
    kb = getattr(cb.message, "reply_markup", None)
    for row in getattr(kb, "inline_keyboard", None) or []:
        for b in row:
            if b.callback_data == cb.data:
                text = b.text.replace("🗑", "").strip()
                return text or "это"
    return "это"


class ConfirmDelete(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[CallbackQuery, dict[str, Any]], Awaitable[Any]],
        event: CallbackQuery,
        data: dict[str, Any],
    ) -> Any:
        raw = event.data or ""
        if raw == NO:
            await event.answer("Отменено")
            try:
                await event.message.edit_text("Отменено, ничего не удалено.")
            except Exception:
                pass
            return None
        if raw.startswith(YES):
            original = raw[len(YES):]
            if is_dangerous(original):
                return await handler(event.model_copy(update={"data": original}), data)
            return None  # подделанная кнопка — ничего не делаем
        if is_dangerous(raw) and len((YES + raw).encode()) <= 64:
            await event.answer()
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🗑 Да, удалить", callback_data=YES + raw),
                InlineKeyboardButton(text="Отмена", callback_data=NO),
            ]])
            what = "Очистить свободный запас?" if raw.startswith("ad:clear:") else f"Удалить «{html.escape(_label(event)[:60])}»?"
            await event.message.answer(f"⚠️ {what}\nЭто нельзя отменить.", reply_markup=kb)
            return None
        return await handler(event, data)
