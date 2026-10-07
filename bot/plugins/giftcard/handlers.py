"""/giftcard — управление плагином Gift Card (FazerCards). Только для администратора."""

from __future__ import annotations

import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from ...db import GiftcardMap, GiftcardOrder, SessionFactory
from ...keyboards import cancel_kb, is_cancel, main_menu
from ...services import features as ft
from ...services.sellers import get_or_create_seller
from ..access import is_admin
from . import service as gc
from .client import FazerError

router = Router(name="giftcard")
log = logging.getLogger(__name__)

STATUS_RU = {
    "PROCESSING": "🔄 покупаю",
    "AWAITING": "⏳ жду код",
    "BOUGHT": "📨 отправляю",
    "DELIVERED": "✅ выдано",
    "UNKNOWN": "❓ итог неизвестен",
    "FAILED": "❌ ошибка",
    "NEEDS_CHECK": "⚠️ нужна проверка",
}


class AddMap(StatesGroup):
    lot = State()
    category = State()
    card = State()
    quantity = State()


class ShowOffers(StatesGroup):
    category = State()


class SetKey(StatesGroup):
    key = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _admin_msg(message: Message) -> bool:
    return bool(message.from_user) and is_admin(message.from_user.id)


def _admin_cb(cb: CallbackQuery) -> bool:
    return bool(cb.from_user) and is_admin(cb.from_user.id)


async def _client(sessions: SessionFactory, tg: int):
    async with sessions() as session:
        return gc.make_client(await gc.get_api_key(session, tg))


# Всё в этом роутере — только для админа: остальным команда просто не отвечает.
router.message.filter(_admin_msg)
router.callback_query.filter(_admin_cb)


async def render(sessions: SessionFactory, tg: int) -> tuple[str, InlineKeyboardMarkup]:
    async with sessions() as session:
        enabled = await ft.get_flag(session, tg, gc.ENABLED_KEY, False)
        counts = await gc.stats(session, tg)
        maps = list(await session.scalars(select(GiftcardMap).where(GiftcardMap.seller_tg_id == tg)))
        has_key = bool(await gc.get_api_key(session, tg))
    ok = counts.get("DELIVERED", 0)
    bad = sum(counts.get(s, 0) for s in ("FAILED", "UNKNOWN", "NEEDS_CHECK"))
    busy = sum(counts.get(s, 0) for s in ("PROCESSING", "AWAITING", "BOUGHT"))
    lines = [
        "<b>🎁 Gift Card — FazerCards</b>",
        "",
        "Оплаченный заказ на привязанный лот → покупка карты у поставщика → код покупателю "
        "в чат → заказ отмечается выполненным (как после автовыдачи).",
        "",
        f"<b>Статус:</b> {'🟢 включено' if enabled else '🔴 выключено'}",
        f"<b>API-ключ:</b> {'✅ задан' if has_key else '❌ не задан — нажми «🔑 Ввести API-ключ»'}",
        f"<b>Выдано:</b> {ok}   <b>Ошибок:</b> {bad}   <b>В работе:</b> {busy}",
        "",
        f"<b>Привязки ({len(maps)}):</b>",
    ]
    lines += [
        f"• {html.escape(m.lot_key[:60])} → <code>{html.escape(m.category_id)}</code> / "
        f"<code>{html.escape(m.card_id)}</code> ×{m.quantity}"
        for m in maps
    ] or ["пока нет — добавь кнопкой ниже"]
    rows = [
        [_btn("🔴 Выключить" if enabled else "🟢 Включить", "gc:t")],
        [_btn("🔌 Проверить API", "gc:check"), _btn("📦 Заказы", "gc:orders")],
        [_btn("📋 Каталог", "gc:cats"), _btn("💳 Номиналы категории", "gc:offers")],
        [_btn("➕ Привязать лот", "gc:add")],
        [_btn("🔑 Ввести API-ключ", "gc:key")] + ([_btn("🗑 Удалить ключ", "gc:keydel")] if has_key else []),
    ]
    rows += [[_btn(f"🗑 {m.lot_key[:30]}", f"gc:del:{m.id}")] for m in maps]
    rows.append([_btn("🎁 Выдать по оплаченным заказам", "gc:paid")])
    rows.append([_btn("🧪 Тест (без покупки)", "gc:test")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show(target: Message | CallbackQuery, sessions: SessionFactory) -> None:
    text, kb = await render(sessions, target.from_user.id)
    if isinstance(target, CallbackQuery):
        await target.answer()
        try:
            await target.message.edit_text(text, reply_markup=kb)
            return
        except Exception:
            pass
        await target.message.answer(text, reply_markup=kb)
    else:
        await target.answer(text, reply_markup=kb)


@router.message(Command("giftcard", "giftcards"))
async def cmd_giftcard(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    await _show(message, sessions)


@router.callback_query(F.data == "gc:t")
async def toggle(cb: CallbackQuery, sessions: SessionFactory) -> None:
    tg = cb.from_user.id
    async with sessions() as session:
        cur = await ft.get_flag(session, tg, gc.ENABLED_KEY, False)
        await ft.set_setting(session, tg, gc.ENABLED_KEY, "0" if cur else "1")
        if not cur:
            # заказы, увиденные до включения, не покупаем
            await ft.set_setting(session, tg, gc.ENABLED_AT_KEY, datetime.utcnow().isoformat())
    log.info("GIFTCARD плагин %s админом", "выключен" if cur else "включен")
    await _show(cb, sessions)


@router.callback_query(F.data == "gc:check")
async def check(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await cb.answer("Проверяю…")
    try:
        async with await _client(sessions, cb.from_user.id) as api:
            balance, cur = await api.balance()
        await cb.message.answer(f"🔌 FazerCards: связь есть, баланс <b>{html.escape(balance)} {html.escape(cur)}</b>.")
    except FazerError as e:
        await cb.message.answer(f"🔌 FazerCards: ошибка — <code>{html.escape(str(e))[:300]}</code>")


@router.callback_query(F.data == "gc:cats")
async def categories(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await cb.answer("Загружаю…")
    try:
        async with await _client(sessions, cb.from_user.id) as api:
            items = await api.categories()
    except FazerError as e:
        await cb.message.answer(f"❌ <code>{html.escape(str(e))[:300]}</code>")
        return
    if not items:
        await cb.message.answer("Каталог пуст.")
        return
    lines = [f"<b>Категории Gift Card ({len(items)}):</b>", ""]
    for i in items[:80]:
        lines.append(f"• {html.escape(str(i.get('name')))} — <code>{html.escape(str(i.get('category_id')))}</code>")
    if len(items) > 80:
        lines.append(f"…и ещё {len(items) - 80}")
    lines += ["", "Номиналы: «💳 Номиналы категории» и пришли её ID."]
    await cb.message.answer("\n".join(lines)[:4000])


@router.callback_query(F.data == "gc:offers")
async def offers_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(ShowOffers.category)
    await cb.answer()
    await cb.message.answer("Пришли ID категории (category_id) из «📋 Каталог»:", reply_markup=cancel_kb())


async def _offers_text(category_id: str, sessions: SessionFactory, tg: int) -> str:
    async with await _client(sessions, tg) as api:
        data = await api.offers(category_id)
    offers = data.get("offers") or []
    lines = [f"<b>{html.escape(str(data.get('name') or category_id))}</b> — <code>{html.escape(category_id)}</code>", ""]
    for o in offers[:60]:
        lines.append(
            f"• {html.escape(str(o.get('name')))}: card_id <code>{html.escape(str(o.get('card_id')))}</code>, "
            f"${html.escape(str(o.get('price_usd')))}, в наличии {o.get('stock')}"
        )
    return "\n".join(lines or ["нет номиналов"])[:4000]


@router.message(ShowOffers.category, F.text, ~F.text.func(is_cancel))
async def offers_show(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    try:
        text = await _offers_text(message.text.strip(), sessions, message.from_user.id)
    except FazerError as e:
        text = f"❌ <code>{html.escape(str(e))[:300]}</code>"
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(text, reply_markup=main_menu(seller.is_connected))


# ----- API-ключ -----


@router.callback_query(F.data == "gc:key")
async def key_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetKey.key)
    await cb.answer()
    await cb.message.answer(
        "Пришли API-ключ FazerCards (начинается с <code>fc_</code>).\n"
        "Сообщение с ключом я сразу удалю, ключ сохраню в зашифрованном виде.",
        reply_markup=cancel_kb(),
    )


@router.message(SetKey.key, F.text, ~F.text.func(is_cancel))
async def key_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    key = message.text.strip()
    try:
        await message.delete()  # не оставляем ключ в переписке
    except Exception:
        pass
    if not key.startswith("fc_") or len(key) < 10 or " " in key:
        await message.answer("Это не похоже на ключ FazerCards (должен начинаться с fc_). Пришли ещё раз или «Отмена».")
        return
    await state.clear()
    tg = message.from_user.id
    try:
        async with gc.make_client(key) as api:
            balance, cur = await api.balance()
        check_line = f"✅ Ключ работает, баланс {html.escape(balance)} {html.escape(cur)}."
    except FazerError as e:
        check_line = f"⚠️ Ключ сохранён, но проверка не прошла: <code>{html.escape(str(e))[:200]}</code>"
    async with sessions() as session:
        await gc.set_api_key(session, tg, key)
        seller = await get_or_create_seller(session, message.from_user)
    log.info("GIFTCARD API-ключ обновлён админом")
    await message.answer(f"🔑 Ключ сохранён.\n{check_line}", reply_markup=main_menu(seller.is_connected))
    await _show(message, sessions)


@router.callback_query(F.data == "gc:keydel")
async def key_delete(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        await gc.set_api_key(session, cb.from_user.id, "")
    log.info("GIFTCARD API-ключ удалён админом")
    await _show(cb, sessions)


# ----- привязки -----


@router.callback_query(F.data == "gc:add")
async def add_start(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddMap.lot)
    await cb.answer()
    await cb.message.answer(
        "Шаг 1/4. Пришли <b>ссылку на лот</b> Playerok или его <b>точное название</b>:",
        reply_markup=cancel_kb(),
    )


MAX_BUTTONS = 30


def _pick_kb(items: list[tuple[str, str]], prefix: str) -> InlineKeyboardMarkup:
    """Кнопки выбора: в callback только номер в списке, сам список — в FSM."""
    rows = [[_btn(label[:60], f"{prefix}:{i}")] for i, (_, label) in enumerate(items[:MAX_BUTTONS])]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _ask_category(message: Message, state: FSMContext, cats: list[tuple[str, str]], query: str = "") -> None:
    found = [c for c in cats if query.lower() in c[1].lower()] if query else cats
    if not found:
        await message.answer("Ничего не нашёл. Напиши другую часть названия, например <code>steam</code>.")
        return
    await state.update_data(cats_view=found[:MAX_BUTTONS])
    more = (
        f"\nПоказаны первые {MAX_BUTTONS} из {len(found)} — напиши часть названия для поиска "
        "(например <code>steam</code>)."
        if len(found) > MAX_BUTTONS else "\nМожно написать часть названия для поиска."
    )
    await message.answer(
        f"Шаг 2/4. Выбери <b>карту</b> (категорию FazerCards):{more}",
        reply_markup=_pick_kb(found, "gc:pc"),
    )


@router.message(AddMap.lot, F.text, ~F.text.func(is_cancel))
async def add_lot(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    raw = message.text.strip()
    key = raw.split("/products/", 1)[1].split("?")[0].strip("/") if "/products/" in raw else raw
    await state.update_data(lot=key[:255])
    await state.set_state(AddMap.category)
    try:
        async with await _client(sessions, message.from_user.id) as api:
            items = await api.categories()
    except FazerError as e:
        await message.answer(
            f"Не смог загрузить каталог FazerCards: <code>{html.escape(str(e))[:200]}</code>\n"
            "Проверь ключ («🔌 Проверить API») и начни заново."
        )
        await state.clear()
        return
    cats = [(str(i.get("category_id")), str(i.get("name") or i.get("category_id"))) for i in items if i.get("category_id")]
    await state.update_data(cats=cats)
    await _ask_category(message, state, cats)


@router.message(AddMap.category, F.text, ~F.text.func(is_cancel))
async def add_category_search(message: Message, state: FSMContext) -> None:
    cats = (await state.get_data()).get("cats") or []
    await _ask_category(message, state, cats, message.text.strip())


@router.callback_query(AddMap.category, F.data.startswith("gc:pc:"))
async def add_category_pick(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    view = (await state.get_data()).get("cats_view") or []
    idx = int(cb.data.split(":")[2])
    if idx >= len(view):
        await cb.answer("Список устарел, выбери ещё раз", show_alert=True)
        return
    category_id, name = view[idx]
    await cb.answer()
    try:
        async with await _client(sessions, cb.from_user.id) as api:
            data = await api.offers(category_id)
    except FazerError as e:
        await cb.message.answer(f"Не смог загрузить номиналы: <code>{html.escape(str(e))[:200]}</code>")
        return
    offers = [
        (str(o.get("card_id")),
         f"{o.get('name')} — ${o.get('price_usd')} (в наличии {o.get('stock')})")
        for o in data.get("offers") or [] if o.get("card_id") is not None
    ]
    if not offers:
        await cb.message.answer(f"В «{html.escape(name)}» сейчас нет номиналов. Выбери другую карту.")
        return
    await state.update_data(category=category_id, category_name=name, offers=offers[:MAX_BUTTONS])
    await state.set_state(AddMap.card)
    await cb.message.answer(
        f"Шаг 3/4. <b>{html.escape(name)}</b> — выбери номинал:",
        reply_markup=_pick_kb(offers, "gc:po"),
    )


@router.callback_query(AddMap.card, F.data.startswith("gc:po:"))
async def add_card_pick(cb: CallbackQuery, state: FSMContext) -> None:
    offers = (await state.get_data()).get("offers") or []
    idx = int(cb.data.split(":")[2])
    if idx >= len(offers):
        await cb.answer("Список устарел, выбери ещё раз", show_alert=True)
        return
    card_id, label = offers[idx]
    await state.update_data(card=card_id, card_label=label)
    await state.set_state(AddMap.quantity)
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[_btn(str(n), f"gc:pq:{n}") for n in (1, 2, 3, 5)]])
    await cb.message.answer(
        f"Шаг 4/4. Номинал: {html.escape(label)}\nСколько кодов выдавать за один заказ? "
        "Нажми кнопку или напиши число:",
        reply_markup=kb,
    )


async def _save_map(user, state: FSMContext, sessions: SessionFactory, qty: int, reply: Message) -> None:
    data = await state.get_data()
    await state.clear()
    async with sessions() as session:
        session.add(GiftcardMap(
            seller_tg_id=user.id, lot_key=data["lot"], category_id=data["category"],
            card_id=data["card"], quantity=qty,
        ))
        await session.commit()
        seller = await get_or_create_seller(session, user)
    await reply.answer(
        f"✅ Привязано: «{html.escape(data['lot'][:60])}» → {html.escape(data.get('category_name', ''))}, "
        f"{html.escape(data.get('card_label', data['card']))} ×{qty}",
        reply_markup=main_menu(seller.is_connected),
    )
    text, kb = await render(sessions, user.id)
    await reply.answer(text, reply_markup=kb)


@router.callback_query(AddMap.quantity, F.data.startswith("gc:pq:"))
async def add_quantity_pick(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await cb.answer()
    await _save_map(cb.from_user, state, sessions, int(cb.data.split(":")[2]), cb.message)


@router.message(AddMap.quantity, F.text, ~F.text.func(is_cancel))
async def add_quantity(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 100:
        await message.answer("Нужно число от 1 до 100.")
        return
    await _save_map(message.from_user, state, sessions, int(text), message)


@router.callback_query(F.data.startswith("gc:del:"))
async def del_map(cb: CallbackQuery, sessions: SessionFactory) -> None:
    map_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        await session.execute(
            delete(GiftcardMap).where(GiftcardMap.id == map_id, GiftcardMap.seller_tg_id == cb.from_user.id)
        )
        await session.commit()
    await _show(cb, sessions)


# ----- заказы -----


@router.callback_query(F.data == "gc:orders")
async def orders(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        rows = list(await session.scalars(
            select(GiftcardOrder).where(GiftcardOrder.seller_tg_id == cb.from_user.id)
            .order_by(GiftcardOrder.id.desc()).limit(15)
        ))
    await cb.answer()
    if not rows:
        await cb.message.answer("Заказов Gift Card пока не было.")
        return
    lines = ["<b>Последние заказы Gift Card:</b>", ""]
    kb: list[list[InlineKeyboardButton]] = []
    for o in rows:
        line = f"• {html.escape(o.item_name[:40])} — {html.escape(o.buyer or '—')}: {STATUS_RU.get(o.status, o.status)}"
        if o.provider_order_id:
            line += f" ({html.escape(o.provider_order_id)})"
        if o.error and o.status != "DELIVERED":
            line += f"\n   <i>{html.escape(o.error[:200])}</i>"
        lines.append(line)
        if o.status in ("FAILED", "UNKNOWN") or (o.status == "NEEDS_CHECK" and o.codes_enc):
            kb.append([_btn(f"🔁 Повторить: {o.item_name[:25]}", f"gc:retry:{o.id}")])
    await cb.message.answer("\n".join(lines)[:4000], reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)


@router.callback_query(F.data.startswith("gc:retry:"))
async def retry(cb: CallbackQuery, sessions: SessionFactory) -> None:
    order_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        order = await session.scalar(
            select(GiftcardOrder).where(GiftcardOrder.id == order_id, GiftcardOrder.seller_tg_id == cb.from_user.id)
        )
        text = "Заказ не найден." if order is None else await gc.retry(session, order)
    await cb.answer(text, show_alert=True)


# ----- ручная выдача -----


@router.callback_query(F.data == "gc:paid")
async def paid_list(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        pending = await gc.pending_paid_deals(session, cb.from_user.id)
    await cb.answer()
    if not pending:
        await cb.message.answer(
            "Нет оплаченных невыданных заказов на привязанные лоты за последние 24 ч.\n"
            "Если заказ точно есть — проверь, что лот привязан (название должно совпадать)."
        )
        return
    lines = ["<b>Оплачены, но код не выдан:</b>", ""]
    kb = []
    for st, m in pending[:10]:
        lines.append(f"• {html.escape((st.item_name or '')[:50])} — {html.escape(st.buyer or 'покупатель')}")
        kb.append([_btn(f"🎁 Выдать: {(st.buyer or 'покупатель')[:20]} · {(st.item_name or '')[:20]}",
                        f"gc:give:{st.deal_id}"[:64])])
    lines += ["", "Нажми — бот купит карту у FazerCards и отправит код покупателю в чат (≤30 с), "
              "затем отметит заказ выполненным."]
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("gc:give:"))
async def paid_give(cb: CallbackQuery, sessions: SessionFactory) -> None:
    deal_id = cb.data.split(":", 2)[2]
    async with sessions() as session:
        if not await gc.get_api_key(session, cb.from_user.id):
            await cb.answer("Сначала введи API-ключ", show_alert=True)
            return
        result = await gc.start_manual(session, cb.from_user.id, deal_id)
    if result != "ok":
        await cb.answer(result, show_alert=True)
        return
    await cb.answer("Покупаю и выдаю — придёт уведомление", show_alert=True)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


# ----- тест без покупки -----


@router.callback_query(F.data == "gc:test")
async def test(cb: CallbackQuery, sessions: SessionFactory) -> None:
    """Проверяет ключ, баланс и каждую привязку (номинал есть, в наличии, цена) — ничего не покупая."""
    await cb.answer("Проверяю…")
    async with sessions() as session:
        maps = list(await session.scalars(select(GiftcardMap).where(GiftcardMap.seller_tg_id == cb.from_user.id)))
    lines = ["<b>🧪 Тест Gift Card (без покупки)</b>", ""]
    try:
        async with await _client(sessions, cb.from_user.id) as api:
            balance, cur = await api.balance()
            lines.append(f"✅ Ключ работает, баланс {html.escape(balance)} {html.escape(cur)}")
            for m in maps:
                try:
                    data = await api.offers(m.category_id)
                except FazerError as e:
                    lines.append(f"❌ {html.escape(m.lot_key[:40])}: {html.escape(str(e))[:150]}")
                    continue
                offer = next((o for o in data.get("offers") or [] if str(o.get("card_id")) == m.card_id), None)
                if offer is None:
                    lines.append(f"❌ {html.escape(m.lot_key[:40])}: card_id {html.escape(m.card_id)} нет в категории")
                    continue
                stock = offer.get("stock")
                mark = "✅" if isinstance(stock, int) and stock >= m.quantity else "⚠️"
                lines.append(
                    f"{mark} {html.escape(m.lot_key[:40])}: {html.escape(str(offer.get('name')))}, "
                    f"${html.escape(str(offer.get('price_usd')))} ×{m.quantity}, в наличии {stock}"
                )
    except FazerError as e:
        lines.append(f"❌ {html.escape(str(e))[:300]}")
    if not maps:
        lines.append("Привязок нет — добавь «➕ Привязать лот».")
    await cb.message.answer("\n".join(lines)[:4000])


@router.message(StateFilter(AddMap, ShowOffers, SetKey), F.text.func(is_cancel))
@router.message(StateFilter(AddMap, ShowOffers, SetKey), Command("cancel"))
async def cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
