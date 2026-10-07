"""Экран «📉 Снижение цен»: правила, проверка вручную."""

from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from ..crypto import TokenCipher
from ..db import PriceRule, Seller, SessionFactory
from ..keyboards import cancel_kb, is_cancel, main_menu
from ..playerok import AuthRequired, PlayerokClient, PlayerokError
from ..services import features as ft
from ..services import pricing
from ..services.sellers import get_or_create_seller

router = Router(name="pricing")
log = logging.getLogger(__name__)


class NominalInput(StatesGroup):
    link = State()
    divisor = State()
    costs = State()


NOMINAL_LINK_KEY = "nominal_lot_ref"
NOMINAL_DIV_KEY = "nominal_divisor"
NOMINAL_COSTS_KEY = "nominal_costs"


class AddPriceRule(StatesGroup):
    lot = State()
    competitor = State()
    step = State()
    minimum = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _num(text: str) -> float | None:
    try:
        v = float(text.replace(" ", "").replace(",", ".").rstrip("₽р"))
    except ValueError:
        return None
    return v if v >= 0 else None


async def render_dumping(
    sessions: SessionFactory, seller: Seller, feature: ft.Feature
) -> tuple[str, InlineKeyboardMarkup]:
    tg = seller.tg_id
    async with sessions() as session:
        enabled = await ft.is_enabled(session, tg, feature)
        interval = await ft.get_param(session, tg, "dumping_interval_min")
        rules = list(await session.scalars(select(PriceRule).where(PriceRule.seller_tg_id == tg)))
    lines = [
        f"<b>{feature.title}</b>",
        "",
        feature.description,
        "",
        f"<b>Статус:</b> {'🟢 Включено' if enabled else '🔴 Выключено'}",
        f"<b>Проверка:</b> раз в {interval} мин",
        "",
        f"<b>Лоты ({len(rules)}):</b>",
    ]
    for r in rules:
        lines.append(
            f"• <b>{html.escape(r.lot_key[:60])}</b>\n"
            f"   сравниваю с «{html.escape(r.competitor_kw)}», шаг {r.step:g} ₽, минимум {r.min_price:g} ₽"
            + (f"\n   <i>{html.escape(r.last_note)}</i>" if r.last_note else "")
        )
    if not rules:
        lines.append("пока нет — добавь кнопкой ниже")
    lines += ["", feature.note]
    rows = [
        [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")],
        [_btn(f"⏱ Проверять раз в {interval} мин", f"f:{feature.key}:p:dumping_interval_min")],
        [_btn("➕ Добавить лот", "dp:add"), _btn("▶️ Проверить сейчас", "dp:run")],
    ]
    rows += [[_btn(f"🗑 {r.lot_key[:30]}", f"dp:del:{r.id}")] for r in rules]
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_screen(message: Message, sessions: SessionFactory, user) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
    text, kb = await render_dumping(sessions, seller, ft.FEATURE_BY_KEY["dumping"])
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "dp:add")
async def add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddPriceRule.lot)
    await cb.answer()
    await cb.message.answer(
        "Шаг 1/4. Пришли <b>ссылку на свой лот</b> или его название:", reply_markup=cancel_kb()
    )


@router.message(AddPriceRule.lot, F.text, ~F.text.func(is_cancel))
async def add_lot(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    key = raw.split("/products/", 1)[1].split("?")[0].strip("/") if "/products/" in raw else raw
    await state.update_data(lot=key[:255])
    await state.set_state(AddPriceRule.competitor)
    await message.answer(
        "Шаг 2/4. <b>С какими лотами конкурентов сравнивать?</b> Ключевые слова, которые есть "
        "в их названиях, например <code>100 робукс</code>.\n"
        "Числа сравниваются целиком (100 ≠ 1000), слова — по началу («робукс» = «робуксов»)."
    )


@router.message(AddPriceRule.competitor, F.text, ~F.text.func(is_cancel))
async def add_competitor(message: Message, state: FSMContext) -> None:
    await state.update_data(competitor=message.text.strip()[:255])
    await state.set_state(AddPriceRule.step)
    await message.answer("Шаг 3/4. На сколько рублей быть дешевле самого дешёвого конкурента? Например <code>1</code>:")


@router.message(AddPriceRule.step, F.text, ~F.text.func(is_cancel))
async def add_step(message: Message, state: FSMContext) -> None:
    step = _num(message.text)
    if step is None or step == 0:
        await message.answer("Нужно число больше 0, например 1 или 0,5.")
        return
    await state.update_data(step=step)
    await state.set_state(AddPriceRule.minimum)
    await message.answer(
        "Шаг 4/4. <b>Минимальная цена</b> (для покупателя, как на сайте) — ниже бот не опустит. "
        "Например <code>80</code>:"
    )


@router.message(AddPriceRule.minimum, F.text, ~F.text.func(is_cancel))
async def add_minimum(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    minimum = _num(message.text)
    if minimum is None:
        await message.answer("Нужно число, например 80.")
        return
    data = await state.get_data()
    await state.clear()
    async with sessions() as session:
        session.add(PriceRule(
            seller_tg_id=message.from_user.id, lot_key=data["lot"], competitor_kw=data["competitor"],
            step=data["step"], min_price=minimum,
        ))
        await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Лот добавлен. Нажми «▶️ Проверить сейчас», чтобы увидеть результат.",
                         reply_markup=main_menu(seller.is_connected))
    await _send_screen(message, sessions, message.from_user)


@router.callback_query(F.data.startswith("dp:del:"))
async def del_rule(cb: CallbackQuery, sessions: SessionFactory) -> None:
    rule_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        await session.execute(
            delete(PriceRule).where(PriceRule.id == rule_id, PriceRule.seller_tg_id == cb.from_user.id)
        )
        await session.commit()
        seller = await get_or_create_seller(session, cb.from_user)
    text, kb = await render_dumping(sessions, seller, ft.FEATURE_BY_KEY["dumping"])
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer("Удалено")


@router.callback_query(F.data == "dp:run")
async def run_now(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Проверяю цены…")
    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            results = await pricing.run(cb.bot, sessions, seller, client, force=True)
    except (AuthRequired, PlayerokError) as e:
        await cb.message.answer(f"⚠️ Playerok: <code>{html.escape(str(e))[:300]}</code>")
        return
    if not results:
        await cb.message.answer("Нет лотов для проверки — добавь «➕ Добавить лот».")
        return
    lines = ["<b>📉 Проверка цен:</b>", ""]
    lines += [f"{'✅' if ch else '•'} {html.escape(r.lot_key[:50])}: {html.escape(note)}" for r, note, ch in results]
    await cb.message.answer("\n".join(lines)[:4000])


# ----- цены по номиналам -----


NOMINAL_TIMEOUT = 180  # секунд на весь отчёт
_NOMINAL_RUNNING: set[int] = set()


def _nominal_kb(divisor: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [_btn("🔄 Обновить", "dp:nomrun")],
        [_btn("💵 Закупка", "dp:nomcost"), _btn(f"➗ Курс: {divisor:g}", "dp:nomdiv")],
        [_btn("🔗 Другая категория", "dp:nomlink")],
        [_btn("‹ К калькулятору", "pc")],
    ])


async def _nominal(message: Message, sessions: SessionFactory, cipher: TokenCipher, user) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
        ref = await ft.get_setting(session, user.id, NOMINAL_LINK_KEY, "")
        try:
            divisor = float(await ft.get_setting(session, user.id, NOMINAL_DIV_KEY, "104"))
        except ValueError:
            divisor = 104.0
        costs = pricing.parse_costs(await ft.get_setting(session, user.id, NOMINAL_COSTS_KEY, ""))
    if not seller.is_connected:
        await message.answer("Аккаунт Playerok не подключён.")
        return
    if user.id in _NOMINAL_RUNNING:
        await message.answer("⏳ Отчёт уже собирается — дождись его.")
        return
    _NOMINAL_RUNNING.add(user.id)
    status = await message.answer("🔎 Собираю цены конкурентов…")
    last_edit = [0.0]

    async def show(text: str, force: bool = False) -> None:
        # Правим одно сообщение не чаще раза в 3 с, чтобы не упереться в лимиты Telegram.
        import time

        if not force and time.monotonic() - last_edit[0] < 3:
            return
        last_edit[0] = time.monotonic()
        try:
            await status.edit_text(text)
        except Exception:
            pass

    async def on_page(page: int, pages: int, count: int) -> None:
        await show(f"🔎 Собираю цены конкурентов… страница {page}/{pages}, лотов: {count}")

    async def on_wait(note: str) -> None:
        await show(f"🔎 Собираю цены конкурентов… ⏳ {note}", force=True)

    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            client.on_wait = on_wait
            text = await asyncio.wait_for(
                pricing.nominal_report(client, ref, divisor or 104.0, costs=costs,
                                       own_user_id=seller.playerok_id, on_page=on_page),
                timeout=NOMINAL_TIMEOUT,
            )
    except asyncio.TimeoutError:
        text = ("⚠️ Playerok слишком долго не отвечал (лимит запросов). Попробуй «🔄 Обновить» через "
                "пару минут.")
    except (AuthRequired, PlayerokError) as e:
        text = f"⚠️ Playerok: <code>{html.escape(str(e))[:300]}</code>"
    except Exception as e:  # чтобы бот не молчал при любой ошибке
        log.exception("выгода по рынку: сбой")
        text = f"⚠️ Ошибка: <code>{html.escape(e.__class__.__name__)}: {html.escape(str(e))[:200]}</code>"
    finally:
        _NOMINAL_RUNNING.discard(user.id)
    try:
        await status.delete()
    except Exception:
        pass
    await message.answer(text[:4000], reply_markup=_nominal_kb(divisor))


@router.callback_query(F.data == "dp:nom")
async def nominal_start(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        ref = await ft.get_setting(session, cb.from_user.id, NOMINAL_LINK_KEY, "")
    await cb.answer()
    if ref:
        await _nominal(cb.message, sessions, cipher, cb.from_user)
        return
    await nominal_ask_link(cb, state)


@router.callback_query(F.data == "dp:nomrun")
async def nominal_refresh(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    await cb.answer("Обновляю…")
    await _nominal(cb.message, sessions, cipher, cb.from_user)


@router.callback_query(F.data == "dp:nomlink")
async def nominal_ask_link(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NominalInput.link)
    try:
        await cb.answer()
    except Exception:
        pass  # уже ответили (вызов из nominal_start)
    await cb.message.answer(
        "Пришли ссылку на любой лот нужной категории и способа получения "
        "(например, свой лот «робуксы промокодом»):", reply_markup=cancel_kb()
    )


@router.message(NominalInput.link, F.text, ~F.text.func(is_cancel))
async def nominal_save_link(message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    if "/products/" not in message.text:
        await message.answer("Нужна ссылка вида https://playerok.com/products/…")
        return
    await state.clear()
    async with sessions() as session:
        await ft.set_setting(session, message.from_user.id, NOMINAL_LINK_KEY, message.text.strip()[:500])
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Запомнил.", reply_markup=main_menu(seller.is_connected))
    await _nominal(message, sessions, cipher, message.from_user)


@router.callback_query(F.data == "dp:nomcost")
async def nominal_ask_costs(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    async with sessions() as session:
        cur = await ft.get_setting(session, cb.from_user.id, NOMINAL_COSTS_KEY, "")
    await state.set_state(NominalInput.costs)
    await cb.answer()
    await cb.message.answer(
        "Пришли закупочную цену в $ для каждого номинала, по строке: номинал и цена.\n"
        "Например:\n<code>50 0.48\n100 0.95\n500 4.6</code>"
        + (f"\n\nСейчас:\n<code>{html.escape(cur)}</code>" if cur else ""),
        reply_markup=cancel_kb(),
    )


@router.message(NominalInput.costs, F.text, ~F.text.func(is_cancel))
async def nominal_save_costs(message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    costs = pricing.parse_costs(message.text)
    if not costs:
        await message.answer("Не понял. Формат: <code>100 0.95</code> — по строке на номинал.")
        return
    await state.clear()
    text = "\n".join(f"{n} {c:g}" for n, c in sorted(costs.items()))
    async with sessions() as session:
        await ft.set_setting(session, message.from_user.id, NOMINAL_COSTS_KEY, text)
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(f"✅ Закупка сохранена ({len(costs)} номиналов).", reply_markup=main_menu(seller.is_connected))
    await _nominal(message, sessions, cipher, message.from_user)


@router.callback_query(F.data == "dp:nomdiv")
async def nominal_ask_div(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NominalInput.divisor)
    await cb.answer()
    await cb.message.answer("Курс: на сколько делить цену в ₽, чтобы получить $. Например <code>104</code>:", reply_markup=cancel_kb())


@router.message(NominalInput.divisor, F.text, ~F.text.func(is_cancel))
async def nominal_save_div(message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    value = _num(message.text)
    if not value:
        await message.answer("Нужно число больше 0, например 104.")
        return
    await state.clear()
    async with sessions() as session:
        await ft.set_setting(session, message.from_user.id, NOMINAL_DIV_KEY, f"{value:g}")
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(f"✅ Делитель: {value:g}", reply_markup=main_menu(seller.is_connected))
    await _nominal(message, sessions, cipher, message.from_user)


@router.message(StateFilter(AddPriceRule, NominalInput), F.text.func(is_cancel))
@router.message(StateFilter(AddPriceRule, NominalInput), Command("cancel"))
async def cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
