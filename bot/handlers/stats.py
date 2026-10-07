from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

import html

from ..crypto import TokenCipher
from sqlalchemy import delete, select

from ..db import ProfitRule, SessionFactory
from ..keyboards import BTN_STATS, cancel_kb, is_cancel, main_menu
from ..playerok import AuthRequired, PlayerokError
from ..services.analytics import build_report, profit_report
from ..services.history import import_history
from ..services.sellers import disconnect_seller, get_or_create_seller

router = Router(name="stats")

REFRESH_KB = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="stats:refresh")],
        [InlineKeyboardButton(text="🧮 Калькулятор прибыли", callback_data="pc")],
        [InlineKeyboardButton(text="📥 Загрузить историю с Playerok", callback_data="stats:import")],
    ]
)


class ProfitInput(StatesGroup):
    keyword = State()
    cost = State()


CURRENCIES = {"$": "$", "usd": "$", "₽": "₽", "р": "₽", "руб": "₽", "rub": "₽", "€": "€", "eur": "€"}


def parse_money(text: str) -> tuple[float, str] | None:
    """«0.05$», «$0,05», «5 ₽», «5 руб» → (сумма, валюта). Без валюты — доллары."""
    t = text.strip().lower().replace(" ", "").replace(",", ".")
    cur = "$"
    for suffix, sym in sorted(CURRENCIES.items(), key=lambda x: -len(x[0])):
        if t.endswith(suffix) or t.startswith(suffix):
            cur = sym
            t = t[len(suffix):] if t.startswith(suffix) else t[: -len(suffix)]
            break
    try:
        value = float(t)
    except ValueError:
        return None
    return (value, cur) if value >= 0 else None


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


# ===================== калькулятор прибыли =====================


async def _profit_screen(sessions: SessionFactory, tg: int) -> tuple[str, InlineKeyboardMarkup]:
    async with sessions() as session:
        text = await profit_report(session, tg)
        rules = list(await session.scalars(select(ProfitRule).where(ProfitRule.seller_tg_id == tg)))
    btn = InlineKeyboardButton
    rows = [
        [btn(text="➕ Добавить товар", callback_data="pc:add"), btn(text="🔄 Пересчитать", callback_data="pc")],
    ]
    rows += [[btn(text=f"🗑 {r.keyword[:30]}", callback_data=f"pc:del:{r.id}")] for r in rules]
    rows.append([btn(text="‹ К аналитике", callback_data="stats:refresh")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "pc")
async def profit_show(cb: CallbackQuery, sessions: SessionFactory) -> None:
    text, kb = await _profit_screen(sessions, cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass
    await cb.answer()


@router.callback_query(F.data == "pc:add")
async def profit_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ProfitInput.keyword)
    await cb.answer()
    await cb.message.answer(
        "Пришли название лота или ключевое слово из него, например <code>50 робуксов</code>.\n"
        "Регистр, эмодзи и знаки не важны.",
        reply_markup=cancel_kb(),
    )


@router.message(ProfitInput.keyword, F.text, ~F.text.func(is_cancel))
async def profit_keyword(message: Message, state: FSMContext) -> None:
    await state.update_data(keyword=message.text.strip()[:255])
    await state.set_state(ProfitInput.cost)
    await message.answer(
        "Чистая прибыль с одной продажи. Например <code>0.05$</code> или <code>5₽</code> "
        "(без валюты — доллары):"
    )


@router.message(ProfitInput.cost, F.text, ~F.text.func(is_cancel))
async def profit_cost(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    parsed = parse_money(message.text)
    if parsed is None:
        await message.answer("Нужна сумма, например 0.05$ или 5₽.")
        return
    value, cur = parsed
    keyword = (await state.get_data())["keyword"]
    await state.clear()
    async with sessions() as session:
        session.add(ProfitRule(seller_tg_id=message.from_user.id, keyword=keyword, cost=value, currency=cur))
        await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Добавлено.", reply_markup=main_menu(seller.is_connected))
    text, kb = await _profit_screen(sessions, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("pc:del:"))
async def profit_del(cb: CallbackQuery, sessions: SessionFactory) -> None:
    rule_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        await session.execute(
            delete(ProfitRule).where(ProfitRule.id == rule_id, ProfitRule.seller_tg_id == cb.from_user.id)
        )
        await session.commit()
    await profit_show(cb, sessions)


@router.message(StateFilter(ProfitInput), F.text.func(is_cancel))
@router.message(StateFilter(ProfitInput), Command("cancel"))
async def profit_cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
