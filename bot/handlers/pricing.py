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
from ..logs import tag
from ..services.sellers import get_or_create_seller

router = Router(name="pricing")
log = logging.getLogger(__name__)


class NominalInput(StatesGroup):
    link = State()
    divisor = State()
    costs = State()
    fazer = State()
    track_min = State()


NOMINAL_LINK_KEY = pricing.NOMINAL_LINK_KEY
NOMINAL_DIV_KEY = pricing.NOMINAL_DIV_KEY
NOMINAL_COSTS_KEY = pricing.NOMINAL_COSTS_KEY


class AddPriceRule(StatesGroup):
    lot = State()
    competitor = State()
    step = State()
    minimum = State()


class CutPrice(StatesGroup):
    lots = State()
    pick = State()
    amount = State()
    confirm = State()


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
            f"   сравниваю с {html.escape(pricing.kw_label(r))}, шаг {r.step:g} ₽, минимум {r.min_price:g} ₽"
            + (f"\n   <i>{html.escape(r.last_note)}</i>" if r.last_note else "")
        )
    if not rules:
        lines.append("пока нет — добавь кнопкой ниже")
    lines += ["", feature.note]
    rows = [
        [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")],
        [_btn(f"⏱ Проверять раз в {interval} мин", f"f:{feature.key}:p:dumping_interval_min")],
        [_btn("➕ Добавить лот", "dp:add"), _btn("▶️ Проверить сейчас", "dp:run")],
        [_btn("💲 Изменить цену своих лотов", "dp:cut")],
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
        "Шаг 2/4. <b>С какими лотами конкурентов сравнивать?</b>\n\n"
        "• Нажми кнопку ниже — бот сам возьмёт <b>название твоего лота</b>: сравнит с лотами "
        "той же категории и способа получения, где то же число (номинал), например 100.\n"
        "• Или пришли ключевые слова из названий конкурентов, например <code>100 робукс</code>. "
        "Числа сравниваются целиком (100 ≠ 1000), слова — по началу («робукс» = «робуксов»).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🏷 По названию моего лота", "dp:byname")],
        ]),
    )


async def _ask_step(message: Message, state: FSMContext) -> None:
    await state.set_state(AddPriceRule.step)
    await message.answer("Шаг 3/4. На сколько рублей быть дешевле самого дешёвого конкурента? Например <code>1</code>:")


@router.message(AddPriceRule.competitor, F.text, ~F.text.func(is_cancel))
async def add_competitor(message: Message, state: FSMContext) -> None:
    await state.update_data(competitor=message.text.strip()[:255])
    await _ask_step(message, state)


@router.callback_query(StateFilter(AddPriceRule.competitor), F.data == "dp:byname")
async def add_competitor_by_name(cb: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(competitor=pricing.NAME_KW)
    await cb.answer("По названию лота")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await _ask_step(cb.message, state)


@router.callback_query(F.data == "dp:byname")
async def add_competitor_by_name_stale(cb: CallbackQuery) -> None:
    await cb.answer("Начни заново: «➕ Добавить лот».", show_alert=True)


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


# ----- ✂️ разовое снижение цены своих лотов -----


def _cut_lines(lots: list[dict], cut: pricing.Cut | None = None) -> tuple[list[str], int]:
    """Строки списка и сколько лотов будет снижено."""
    out, will = [], 0
    for lot in lots[:pricing.MAX_CUT_LOTS]:
        name = html.escape(lot["name"][:50])
        price = lot.get("price")
        if not isinstance(price, (int, float)):
            out.append(f"• {name}")
            will += cut is not None
        elif cut is None:
            out.append(f"• {name} — <b>{pricing._rub(price)}</b>")
        elif why := cut.skip_reason(price):
            out.append(f"⛔ {name}: {pricing._rub(price)} — {html.escape(why)}")
        else:
            will += 1
            out.append(f"✅ {name}: {pricing._rub(price)} → <b>~{pricing._rub(cut.target(price))}</b>")
    return out, will


AMOUNT_HELP = (
    "<code>682</code> — поставить цену 682 ₽\n"
    "<code>-10</code> / <code>+10</code> — дешевле / дороже на 10 ₽\n"
    "<code>-5%</code> / <code>+5%</code> — дешевле / дороже на 5 %"
)


async def _cut_begin(message: Message, state: FSMContext, sessions: SessionFactory, user) -> bool:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
    if not seller.is_connected:
        return False
    await state.set_state(CutPrice.lots)
    await message.answer(
        "💲 <b>Изменить цену своих лотов</b>\n\n"
        "Шаг 1/3. Какие лоты? Пришли:\n"
        "• ключевые слова, например <code>робукс</code> или <code>100 робукс</code> — все твои активные "
        "лоты с ними в названии;\n"
        "• или точное название лота;\n"
        "• или ссылку на лот.",
        reply_markup=cancel_kb(),
    )
    return True


@router.callback_query(F.data == "dp:cut")
async def cut_start(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await cb.answer()
    if not await _cut_begin(cb.message, state, sessions, cb.from_user):
        await cb.message.answer("Аккаунт Playerok не подключён.")


@router.message(Command("price"))
async def cut_command(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    if not await _cut_begin(message, state, sessions, message.from_user):
        await message.answer("Аккаунт Playerok не подключён — нажми «🔑 Войти в Playerok».")


@router.message(CutPrice.lots, F.text, ~F.text.func(is_cancel))
async def cut_lots(message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    if not seller.is_connected:
        await state.clear()
        await message.answer("Аккаунт Playerok не подключён.", reply_markup=main_menu(False))
        return
    wait = await message.answer("🔎 Ищу твои лоты…")
    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            items = await client.my_items(seller.playerok_id or "", limit=96, statuses=["APPROVED"])
    except (AuthRequired, PlayerokError) as e:
        await wait.edit_text(f"⚠️ Playerok: <code>{html.escape(str(e))[:300]}</code>\nПопробуй ещё раз или нажми «Отмена».")
        return
    found = pricing.select_my_lots(items, message.text)
    if not found:
        await wait.edit_text(
            f"Среди активных лотов ({len(items)}) ничего не нашёл по «{html.escape(message.text[:60])}». "
            "Пришли другие слова или нажми «Отмена»."
        )
        return
    premium = [it for it in found if it.is_premium]
    found = [it for it in found if not it.is_premium]
    skipped = (
        f"\n⭐ Премиум-лоты ({len(premium)}) пропускаю — Playerok не даёт менять им цену." if premium else ""
    )
    if not found:
        await wait.edit_text(
            f"Все найденные лоты ({len(premium)}) — с премиум-статусом, Playerok не даёт менять им цену. "
            "Пришли другие слова или нажми «Отмена»."
        )
        return
    found.sort(key=lambda it: -(it.price or 0))
    lots = [{"id": it.id, "name": it.name, "price": it.price} for it in found[:pricing.MAX_CUT_LOTS]]
    more = f"\n…и ещё {len(found) - len(lots)} — за раз не больше {pricing.MAX_CUT_LOTS}." if len(found) > len(lots) else ""
    more += skipped
    if len(lots) == 1:
        await state.update_data(cut_lots=lots)
        await _ask_amount(wait, state, lots, skipped)
        return
    await state.update_data(cut_all=lots, cut_sel=list(range(len(lots))), cut_more=more)
    await state.set_state(CutPrice.pick)
    await wait.edit_text(_pick_text(lots, more), reply_markup=_pick_kb(lots, set(range(len(lots)))))


def _price_key(price) -> str:
    return f"{price:g}" if isinstance(price, (int, float)) else "?"


def _pick_text(lots: list[dict], more: str = "") -> str:
    return (
        f"Нашёл {len(lots)}.{more}\n\n"
        "Шаг 2/3. <b>Какие снижать?</b> Нажми на лот, чтобы снять или поставить ✅. "
        "Кнопки «Только … ₽» оставят лоты с этой ценой. Потом «➡️ Дальше»."
    )


def _pick_kb(lots: list[dict], sel: set[int]) -> InlineKeyboardMarkup:
    rows = [
        [_btn(f"{'✅' if i in sel else '⬜'} {_price_key(lot.get('price'))} ₽ · {lot['name'][:32]}", f"dp:cs:{i}")]
        for i, lot in enumerate(lots)
    ]
    prices: dict[str, int] = {}
    for lot in lots:
        key = _price_key(lot.get("price"))
        prices[key] = prices.get(key, 0) + 1
    if len(prices) > 1:
        rows += [[_btn(f"Только {key} ₽ ({n})", f"dp:csp:{key}")] for key, n in prices.items() if key != "?"][:6]
    rows.append([_btn("✅ Все", "dp:csall"), _btn("⬜ Ни одного", "dp:csnone")])
    rows.append([_btn(f"➡️ Дальше ({len(sel)})", "dp:csok")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_amount(msg: Message, state: FSMContext, lots: list[dict], note: str = "") -> None:
    await state.set_state(CutPrice.amount)
    await msg.edit_text(
        f"Выбрано {len(lots)}:\n" + "\n".join(_cut_lines(lots)[0]) + note + "\n\n"
        "Шаг 3/3. <b>Какая новая цена?</b> Цены — для покупателя, как на сайте.\n" + AMOUNT_HELP
    )


async def _pick_update(cb: CallbackQuery, state: FSMContext, sel: set[int]) -> None:
    data = await state.get_data()
    await state.update_data(cut_sel=sorted(sel))
    await cb.answer()
    try:
        await cb.message.edit_reply_markup(reply_markup=_pick_kb(data.get("cut_all") or [], sel))
    except Exception:
        pass


@router.callback_query(StateFilter(CutPrice.pick), F.data.startswith("dp:cs:"))
async def cut_pick_toggle(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    sel = set(data.get("cut_sel") or [])
    i = int(cb.data.rsplit(":", 1)[1])
    sel ^= {i}
    await _pick_update(cb, state, sel)


@router.callback_query(StateFilter(CutPrice.pick), F.data.startswith("dp:csp:"))
async def cut_pick_price(cb: CallbackQuery, state: FSMContext) -> None:
    key = cb.data.split(":", 2)[2]
    lots = (await state.get_data()).get("cut_all") or []
    await _pick_update(cb, state, {i for i, lot in enumerate(lots) if _price_key(lot.get("price")) == key})


@router.callback_query(StateFilter(CutPrice.pick), F.data.in_({"dp:csall", "dp:csnone"}))
async def cut_pick_all(cb: CallbackQuery, state: FSMContext) -> None:
    lots = (await state.get_data()).get("cut_all") or []
    await _pick_update(cb, state, set(range(len(lots))) if cb.data == "dp:csall" else set())


@router.callback_query(StateFilter(CutPrice.pick), F.data == "dp:csok")
async def cut_pick_ok(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    lots = data.get("cut_all") or []
    chosen = [lots[i] for i in data.get("cut_sel") or [] if i < len(lots)]
    if not chosen:
        await cb.answer("Отметь хотя бы один лот ✅", show_alert=True)
        return
    await state.update_data(cut_lots=chosen)
    await cb.answer()
    await _ask_amount(cb.message, state, chosen)


@router.callback_query(F.data.startswith("dp:cs"))
async def cut_pick_stale(cb: CallbackQuery) -> None:
    await cb.answer("Устарело — начни заново: «💲 Изменить цену своих лотов».", show_alert=True)


@router.message(CutPrice.amount, F.text, ~F.text.func(is_cancel))
async def cut_amount(message: Message, state: FSMContext) -> None:
    cut = pricing.parse_cut(message.text)
    if cut is None:
        await message.answer("Не понял. Пришли число:\n" + AMOUNT_HELP)
        return
    lots = (await state.get_data()).get("cut_lots") or []
    lines, will = _cut_lines(lots, cut)
    if not will:
        await message.answer(
            "\n".join(lines) + "\n\nНечего менять. Пришли другое число:\n" + AMOUNT_HELP
        )
        return
    await state.update_data(cut_kind=cut.kind, cut_value=cut.value, cut_up=cut.up)
    await state.set_state(CutPrice.confirm)
    title = f"Поставить цену {pricing._rub(cut.value)}" if cut.kind == "set" else f"Изменить цену: {cut.label()}"
    await message.answer(
        f"✂️ <b>{title}?</b>\n\n" + "\n".join(lines) + "\n\n"
        "Точная цена будет после пересчёта комиссии Playerok. За раз — не дешевле половины и не "
        "дороже двух текущих цен. Возможно, после смены цены лот уйдёт на проверку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn(f"✅ Да, изменить ({will})", "dp:cutok"), _btn("Отмена", "dp:cutno")],
        ]),
    )


@router.callback_query(F.data == "dp:cutno")
async def cut_cancel(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text("Отменено, цены не трогал.")
    except Exception:
        pass
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    await cb.message.answer("Меню:", reply_markup=main_menu(seller.is_connected))


@router.callback_query(F.data == "dp:cutok")
async def cut_apply(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    data = await state.get_data()
    lots, kind, value = data.get("cut_lots"), data.get("cut_kind"), data.get("cut_value")
    if await state.get_state() != CutPrice.confirm.state or not lots or not kind:
        await cb.answer("Устарело — начни заново: «💲 Изменить цену своих лотов».", show_alert=True)
        return
    await state.clear()
    cut = pricing.Cut(kind, float(value), bool(data.get("cut_up")))
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Меняю…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)  # чтобы не нажать дважды
    except Exception:
        pass
    await cb.message.answer(f"💲 Меняю цены {cut.label()}…", reply_markup=main_menu(True))
    status = await cb.message.answer(f"⏳ 0 из {len(lots)}…")
    lines: list[str] = []
    changed = 0
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        for n, lot in enumerate(lots, 1):
            try:
                note, ok = await pricing.cut_price(client, lot["id"], cut)
            except AuthRequired:
                lines.append("⚠️ Сессия Playerok истекла — остальные не трогал.")
                break
            except PlayerokError as e:
                note, ok = pricing.price_error(str(e)), False
            changed += ok
            lines.append(f"{'✅' if ok else '•'} {html.escape(lot['name'][:50])}: {html.escape(note)}")
            log.info("%s изменение цены вручную «%s»: %s", tag(seller.tg_id), lot["name"][:60], note)
            try:
                await status.edit_text(f"⏳ {n} из {len(lots)}…")
            except Exception:
                pass
    text = f"💲 <b>Изменено: {changed} из {len(lots)}</b> ({cut.label()})\n\n" + "\n".join(lines)
    try:
        await status.edit_text(text[:4000])
    except Exception:
        await cb.message.answer(text[:4000])


# ----- цены по номиналам -----


NOMINAL_TIMEOUT = 300  # секунд на весь отчёт (по запросу на номинал, лимит ~12/мин)
_NOMINAL_RUNNING: set[int] = set()


async def _market_menu(sessions: SessionFactory, tg: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран настроек «Выгода по рынку» — открывается сразу, отчёт — кнопкой «▶️ Посчитать»."""
    from ..plugins.access import is_admin
    from ..services import market_sections as ms

    async with sessions() as session:
        sec = await ms.active(session, tg)
        n_entries = len(await ms.entries(session, sec.id)) if sec else 0
        ref = sec.lot_ref if sec else await ft.get_setting(session, tg, NOMINAL_LINK_KEY, "")
        divisor = await pricing.get_divisor(session, tg)
        fazer_cat = sec.fazer_cat if sec else await ft.get_setting(session, tg, pricing.FAZER_CAT_KEY, "")
        fazer_name = ((sec.fazer_name if sec else await ft.get_setting(session, tg, pricing.FAZER_NAME_KEY, ""))
                      or fazer_cat)
        manual = pricing.parse_costs(await ft.get_setting(session, tg, NOMINAL_COSTS_KEY, "")) if not sec else {}
        track_on = await ft.get_setting(session, tg, pricing.TRACK_KEY, "0") == "1"
        track_min = await ft.get_setting(session, tg, pricing.TRACK_MIN_KEY, "0.01")
    admin = is_admin(tg)
    if sec:
        cost_line = f"★ {html.escape(ms.section_title(sec))} — номиналов: {n_entries}"
        if fazer_cat and admin:
            cost_line += f", FazerCards «{html.escape(fazer_name)}»"
    elif fazer_cat and admin:
        cost_line = f"FazerCards — «{html.escape(fazer_name)}»" + (f" (+ вручную {len(manual)})" if manual else "")
    elif manual:
        cost_line = f"общий список, номиналов: {len(manual)}"
    else:
        cost_line = "не задана — «📦 Разделы закупки»"
    lines = [
        "🌐 <b>Выгода по рынку</b>",
        "",
        "Бот берёт самую низкую цену конкурентов на Playerok по каждому номиналу, делит на курс "
        "и сравнивает с твоей закупкой" + (" (цены FazerCards)" if admin else "") + ".",
        "",
        f"<b>Закупка:</b> {cost_line}",
        f"<b>Категория:</b> {'по лоту ' + html.escape(ref[:60]) if ref else 'не задана — «🔗 Лот для категории»'}",
        f"<b>Курс:</b> ÷{divisor:g}",
        f"<b>Трекер:</b> {'🔔 включён — раз в 30 мин, уведомлю при прибыли ≥ $' + track_min if track_on else '🔕 выключен'}",
    ]
    rows = [[_btn("▶️ Посчитать сейчас", "dp:nomrun")],
            [_btn("📦 Разделы закупки: Apple, PlayStation, Xbox…", "mk:home")]]
    if admin:
        fz_data, off_data = (f"mk:fz:{sec.id}", f"mk:fzx:{sec.id}") if sec else ("dp:fz", "dp:fzoff")
        rows.append([_btn(f"🎁 FazerCards: {fazer_name[:20]}" if fazer_cat else "🎁 Закупка с FazerCards", fz_data)]
                    + ([_btn("✖️", off_data)] if fazer_cat else []))
    rows += [
        [_btn(f"➗ Курс: {divisor:g}", "dp:nomdiv")],
        [_btn("🔔 Трекер: вкл" if track_on else "🔕 Трекер: выкл", "dp:trk"), _btn(f"Порог: ${track_min}", "dp:trkmin")],
        [_btn("🔗 Лот для категории", f"mk:lot:{sec.id}" if sec else "dp:nomlink")],
        [_btn("‹ К калькулятору", "pc")],
    ]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _send_menu(message: Message, sessions: SessionFactory, tg: int) -> None:
    text, kb = await _market_menu(sessions, tg)
    await message.answer(text, reply_markup=kb)


async def _edit_menu(cb: CallbackQuery, sessions: SessionFactory) -> None:
    text, kb = await _market_menu(sessions, cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except Exception:
        await cb.message.answer(text, reply_markup=kb)


REPORT_KB = InlineKeyboardMarkup(inline_keyboard=[
    [_btn("🔄 Обновить", "dp:nomrun"), _btn("⚙️ Настройки", "dp:nom")],
    [_btn("‹ К калькулятору", "pc")],
])


async def _nominal(message: Message, sessions: SessionFactory, cipher: TokenCipher, user) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
        setup = await pricing.market_setup(session, user.id)
        divisor = await pricing.get_divisor(session, user.id)
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

    async def on_step(note: str) -> None:
        await show(f"🔎 Ищу самые дешёвые лоты… {note}")

    kb = REPORT_KB
    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            client.on_wait = on_wait
            lot_ref = setup.lot_ref
            if not lot_ref and setup.section_id:
                # Лот для сравнения не задан — берём из раздела того же бренда или из своих лотов.
                from ..services import market_sections as ms

                async with sessions() as session:
                    sec = await ms.get_section(session, user.id, setup.section_id)
                    if sec is not None:
                        lot_ref = await ms.auto_lot_ref(session, client, user.id, sec, seller.playerok_id or "")
            if not lot_ref:
                text = (f"Не знаю, где на Playerok искать конкурентов «{html.escape(setup.title)}»: у тебя нет "
                        "активных лотов этого бренда. Пришли один раз ссылку на любой такой лот (свой или "
                        "чужой) — «🔗 Лот для сравнения». Для других стран этого бренда повторять не нужно.")
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("🔗 Лот для сравнения", f"mk:lot:{setup.section_id}")],
                    [_btn("‹ Раздел", f"mk:s:{setup.section_id}")],
                ])
            else:
                text = await asyncio.wait_for(
                    pricing.nominal_report(client, lot_ref, divisor, costs=setup.costs, cost_note=setup.note,
                                           own_user_id=seller.playerok_id, on_page=on_page, on_step=on_step,
                                           country_words=setup.country_words, currency_first=setup.currency_first,
                                           title=setup.title, country_key=setup.country_key),
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
    await message.answer(text[:4000], reply_markup=kb)


@router.callback_query(F.data == "dp:nom")
async def nominal_start(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await cb.answer()
    await _edit_menu(cb, sessions)


@router.callback_query(F.data == "dp:nomrun")
async def nominal_refresh(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        setup = await pricing.market_setup(session, cb.from_user.id)
    if not setup.lot_ref and not setup.section_id:  # у раздела лот найдётся сам (_nominal)
        await nominal_ask_link(cb, state)
        return
    await cb.answer("Считаю…")
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
    await _send_menu(message, sessions, message.from_user.id)


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
    await _send_menu(message, sessions, message.from_user.id)


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
    await message.answer(f"✅ Курс: {value:g}", reply_markup=main_menu(seller.is_connected))
    await _send_menu(message, sessions, message.from_user.id)


# ----- закупка с FazerCards -----


@router.callback_query(F.data == "dp:fz")
async def fazer_ask(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    from ..plugins.access import is_admin
    from ..plugins.giftcard import service as gc

    if not is_admin(cb.from_user.id):
        await cb.answer("Только для владельца", show_alert=True)
        return
    async with sessions() as session:
        key = await gc.get_api_key(session, cb.from_user.id)
    if not key:
        await cb.answer("Сначала введи API-ключ FazerCards: /giftcard → 🔑", show_alert=True)
        return
    await state.set_state(NominalInput.fazer)
    await cb.answer()
    await cb.message.answer(
        "С какой картой FazerCards сравнивать? Напиши часть названия, например <code>roblox</code>:",
        reply_markup=cancel_kb(),
    )


@router.message(NominalInput.fazer, F.text, ~F.text.func(is_cancel))
async def fazer_search(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    from ..plugins.giftcard import service as gc
    from ..plugins.giftcard.client import FazerError

    async with sessions() as session:
        key = await gc.get_api_key(session, message.from_user.id)
    try:
        async with gc.make_client(key) as api:
            cats = await api.categories()
    except FazerError as e:
        await message.answer(f"⚠️ FazerCards: <code>{html.escape(str(e))[:200]}</code>")
        return
    q = message.text.strip().lower()
    found = [(str(c.get("category_id")), str(c.get("name") or c.get("category_id")))
             for c in cats if c.get("category_id") and q in str(c.get("name") or "").lower()][:20]
    if not found:
        await message.answer("Ничего не нашёл. Напиши другую часть названия.")
        return
    await state.update_data(fazer_found=found)
    await message.answer(
        "Выбери карту:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[_btn(name[:60], f"dp:fzc:{i}")] for i, (_, name) in enumerate(found)]),
    )


@router.callback_query(NominalInput.fazer, F.data.startswith("dp:fzc:"))
async def fazer_pick(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher) -> None:
    found = (await state.get_data()).get("fazer_found") or []
    idx = int(cb.data.split(":")[2])
    if idx >= len(found):
        await cb.answer("Список устарел, поищи ещё раз", show_alert=True)
        return
    cat_id, name = found[idx]
    section_id = (await state.get_data()).get("section_id")
    await state.clear()
    async with sessions() as session:
        if section_id:  # категория для раздела закупки (📦 Разделы)
            from ..services import market_sections as ms

            sec = await ms.get_section(session, cb.from_user.id, int(section_id))
            if sec is not None:
                sec.fazer_cat, sec.fazer_name = cat_id[:64], name[:64]
                await session.commit()
        else:
            await ft.set_setting(session, cb.from_user.id, pricing.FAZER_CAT_KEY, cat_id)
            await ft.set_setting(session, cb.from_user.id, pricing.FAZER_NAME_KEY, name[:60])
        seller = await get_or_create_seller(session, cb.from_user)
    await cb.answer()
    await cb.message.answer(f"✅ Закупка с FazerCards: {html.escape(name)}", reply_markup=main_menu(seller.is_connected))
    if section_id:
        from .market import send_section

        await send_section(cb.message, sessions, cb.from_user.id, int(section_id))
    else:
        await _send_menu(cb.message, sessions, cb.from_user.id)


@router.callback_query(F.data == "dp:fzoff")
async def fazer_off(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        await ft.set_setting(session, cb.from_user.id, pricing.FAZER_CAT_KEY, "")
        await ft.set_setting(session, cb.from_user.id, pricing.FAZER_NAME_KEY, "")
    await cb.answer("FazerCards отключён — считаю по закупке вручную", show_alert=True)
    await _edit_menu(cb, sessions)


# ----- трекер выгодных номиналов -----


@router.callback_query(F.data == "dp:trk")
async def track_toggle(cb: CallbackQuery, sessions: SessionFactory) -> None:
    tg = cb.from_user.id
    async with sessions() as session:
        on = await ft.get_setting(session, tg, pricing.TRACK_KEY, "0") == "1"
        if not on:
            setup = await pricing.market_setup(session, tg)
            if not setup.lot_ref and not setup.section_id:
                await cb.answer("Сначала «🔗 Лот для категории» — пришли ссылку на лот", show_alert=True)
                return
            if not setup.costs:
                await cb.answer("Сначала задай закупку: «📦 Разделы закупки» или «🎁 FazerCards»", show_alert=True)
                return
        await ft.set_setting(session, tg, pricing.TRACK_KEY, "0" if on else "1")
        await ft.set_setting(session, tg, pricing.TRACK_SEEN_KEY, "")
        await ft.set_setting(session, tg, pricing.TRACK_LAST_KEY, "")
        minimum = await ft.get_setting(session, tg, pricing.TRACK_MIN_KEY, "0.01")
    await cb.answer(
        "🔕 Трекер выключен" if on else
        f"🔔 Трекер включён: проверяю раз в 30 мин и пишу, когда номинал стал выгодным (прибыль ≥ ${minimum})",
        show_alert=True,
    )
    await _edit_menu(cb, sessions)


@router.callback_query(F.data == "dp:trkmin")
async def track_min_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(NominalInput.track_min)
    await cb.answer()
    await cb.message.answer("С какой прибыли в $ на одну продажу уведомлять? Например <code>0.05</code>:",
                            reply_markup=cancel_kb())


@router.message(NominalInput.track_min, F.text, ~F.text.func(is_cancel))
async def track_min_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    value = _num(message.text.replace("$", ""))
    if value is None:
        await message.answer("Нужно число, например 0.05.")
        return
    await state.clear()
    async with sessions() as session:
        await ft.set_setting(session, message.from_user.id, pricing.TRACK_MIN_KEY, f"{value:g}")
        await ft.set_setting(session, message.from_user.id, pricing.TRACK_SEEN_KEY, "")
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(f"✅ Порог трекера: ${value:g}", reply_markup=main_menu(seller.is_connected))
    await _send_menu(message, sessions, message.from_user.id)


@router.message(StateFilter(AddPriceRule, NominalInput, CutPrice), F.text.func(is_cancel))
@router.message(StateFilter(AddPriceRule, NominalInput, CutPrice), Command("cancel"))
async def cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
