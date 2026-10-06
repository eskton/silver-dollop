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


@router.message(AddMap.lot, F.text, ~F.text.func(is_cancel))
async def add_lot(message: Message, state: FSMContext) -> None:
    raw = message.text.strip()
    key = raw.split("/products/", 1)[1].split("?")[0].strip("/") if "/products/" in raw else raw
    await state.update_data(lot=key[:255])
    await state.set_state(AddMap.category)
    await message.answer("Шаг 2/4. ID категории FazerCards (category_id из «📋 Каталог»):")


@router.message(AddMap.category, F.text, ~F.text.func(is_cancel))
async def add_category(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    category = message.text.strip()
    await state.update_data(category=category[:128])
    await state.set_state(AddMap.card)
    try:
        hint = await _offers_text(category, sessions, message.from_user.id)
    except FazerError as e:
        hint = f"(не смог загрузить номиналы: {html.escape(str(e))[:200]})"
    await message.answer(f"{hint}\n\nШаг 3/4. Пришли <b>card_id</b> нужного номинала:")


@router.message(AddMap.card, F.text, ~F.text.func(is_cancel))
async def add_card(message: Message, state: FSMContext) -> None:
    await state.update_data(card=message.text.strip()[:128])
    await state.set_state(AddMap.quantity)
    await message.answer("Шаг 4/4. Сколько кодов выдавать за один заказ? (обычно 1)")


@router.message(AddMap.quantity, F.text, ~F.text.func(is_cancel))
async def add_quantity(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    text = message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 100:
        await message.answer("Нужно число от 1 до 100.")
        return
    data = await state.get_data()
    await state.clear()
    async with sessions() as session:
        session.add(GiftcardMap(
            seller_tg_id=message.from_user.id, lot_key=data["lot"], category_id=data["category"],
            card_id=data["card"], quantity=int(text),
        ))
        await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Привязка добавлена.", reply_markup=main_menu(seller.is_connected))
    await _show(message, sessions)


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
