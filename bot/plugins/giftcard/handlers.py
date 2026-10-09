"""/giftcard — управление плагином Gift Card (FazerCards и AppRoute). Только для администратора."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, select

from ...db import DealState, GiftcardMap, GiftcardOrder, SessionFactory
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
    provider = State()
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
        has_ar = bool(await gc.get_api_key(session, tg, "approute"))
        region = await ft.get_setting(session, tg, gc.AR_REGION_SETTING, "io")
    ok = counts.get("DELIVERED", 0)
    bad = sum(counts.get(s, 0) for s in ("FAILED", "UNKNOWN", "NEEDS_CHECK"))
    busy = sum(counts.get(s, 0) for s in ("PROCESSING", "AWAITING", "BOUGHT"))
    lines = [
        "<b>🎁 Gift Card — FazerCards и AppRoute</b>",
        "",
        "Оплаченный заказ на привязанный лот → покупка карты у поставщика → код покупателю "
        "в чат → заказ отмечается выполненным (как после автовыдачи).",
        "",
        f"<b>Статус:</b> {'🟢 включено' if enabled else '🔴 выключено'}",
        f"<b>Ключ FazerCards:</b> {'✅ задан' if has_key else '❌ не задан'}",
        f"<b>Ключ AppRoute:</b> {'✅ задан' if has_ar else '❌ не задан'} (approute.{region})",
        f"<b>Выдано:</b> {ok}   <b>Ошибок:</b> {bad}   <b>В работе:</b> {busy}",
        "",
        f"<b>Привязки ({len(maps)}):</b>",
    ]
    lines += [
        f"• {html.escape(m.lot_key[:60])} → {gc.PROVIDERS[gc.provider_of(m)]}: "
        + (html.escape(m.card_name) if m.card_name
           else f"<code>{html.escape(m.category_id)}</code> / <code>{html.escape(m.card_id)}</code>")
        + f" ×{m.quantity}"
        for m in maps
    ] or ["пока нет — добавь кнопкой ниже"]
    rows = [
        [_btn("🔴 Выключить" if enabled else "🟢 Включить", "gc:t")],
        [_btn("🔌 Проверить FazerCards", "gc:check"), _btn("🔌 Проверить AppRoute", "gc:archeck")],
        [_btn("📦 История заказов", "gc:orders")],
        [_btn("📋 Каталог Fazer", "gc:cats"), _btn("💳 Номиналы Fazer", "gc:offers")],
        [_btn("➕ Привязать лот", "gc:add")],
        [_btn("🔑 Ключ FazerCards", "gc:key")] + ([_btn("🗑", "gc:keydel")] if has_key else []),
        [_btn("🔑 Ключ AppRoute", "gc:arkey")] + ([_btn("🗑", "gc:arkeydel")] if has_ar else []),
        [_btn(f"🌍 AppRoute: approute.{region} → .{'ru' if region == 'io' else 'io'}", "gc:arreg")],
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


@router.callback_query(F.data == "gc:archeck")
async def ar_check(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await cb.answer("Проверяю…")
    try:
        async with sessions() as session:
            api = await gc.make_approute(session, cb.from_user.id)
        async with api:
            balance, cur = await api.balance()
        await cb.message.answer(f"🔌 AppRoute: связь есть, доступно <b>{html.escape(balance)} {html.escape(cur)}</b>.")
    except FazerError as e:
        await cb.message.answer(
            f"🔌 AppRoute: ошибка — <code>{html.escape(str(e))[:300]}</code>\n"
            "Если ключ постоянный — добавь IP прокси (PLAYEROK_PROXY) в белый список ключа в кабинете AppRoute."
        )


@router.callback_query(F.data == "gc:arreg")
async def ar_region(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        cur = await ft.get_setting(session, cb.from_user.id, gc.AR_REGION_SETTING, "io")
        await ft.set_setting(session, cb.from_user.id, gc.AR_REGION_SETTING, "ru" if cur == "io" else "io")
    await _show(cb, sessions)


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
    await state.update_data(provider="fazer")
    await cb.answer()
    await cb.message.answer(
        "Пришли API-ключ FazerCards (начинается с <code>fc_</code>).\n"
        "Сообщение с ключом я сразу удалю, ключ сохраню в зашифрованном виде.",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data == "gc:arkey")
async def ar_key_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SetKey.key)
    await state.update_data(provider="approute")
    await cb.answer()
    await cb.message.answer(
        "Пришли API-ключ AppRoute (кабинет → API-ключи).\n"
        "Постоянный ключ работает только с разрешённых IP: добавь в его белый список IP своего прокси "
        "(тот же, что в PLAYEROK_PROXY) — бот ходит в AppRoute через него.\n"
        "Сообщение с ключом я сразу удалю, ключ сохраню в зашифрованном виде.",
        reply_markup=cancel_kb(),
    )


async def _ar_key_save(message: Message, state: FSMContext, sessions: SessionFactory, key: str) -> None:
    if len(key) < 10 or " " in key:
        await message.answer("Это не похоже на ключ AppRoute. Пришли ещё раз или «Отмена».")
        return
    await state.clear()
    tg = message.from_user.id
    async with sessions() as session:
        await gc.set_api_key(session, tg, key, "approute")
        api = await gc.make_approute(session, tg)
        seller = await get_or_create_seller(session, message.from_user)
    try:
        async with api:
            balance, cur = await api.balance()
        check_line = f"✅ Ключ работает, доступно {html.escape(balance)} {html.escape(cur)}."
    except FazerError as e:
        check_line = (f"⚠️ Ключ сохранён, но проверка не прошла: <code>{html.escape(str(e))[:200]}</code>\n"
                      "Проверь белый список IP ключа и домен (кнопка «🌍 AppRoute»).")
    log.info("GIFTCARD API-ключ AppRoute обновлён админом")
    await message.answer(f"🔑 Ключ AppRoute сохранён.\n{check_line}", reply_markup=main_menu(seller.is_connected))
    await _show(message, sessions)


@router.message(SetKey.key, F.text, ~F.text.func(is_cancel))
async def key_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    key = message.text.strip()
    try:
        await message.delete()  # не оставляем ключ в переписке
    except Exception:
        pass
    if (await state.get_data()).get("provider") == "approute":
        await _ar_key_save(message, state, sessions, key)
        return
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


@router.callback_query(F.data == "gc:arkeydel")
async def ar_key_delete(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        await gc.set_api_key(session, cb.from_user.id, "", "approute")
    log.info("GIFTCARD API-ключ AppRoute удалён админом")
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
        f"Шаг 2/4. Выбери <b>карту</b> (товар поставщика):{more}",
        reply_markup=_pick_kb(found, "gc:pc"),
    )


@router.message(AddMap.lot, F.text, ~F.text.func(is_cancel))
async def add_lot(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    raw = message.text.strip()
    key = raw.split("/products/", 1)[1].split("?")[0].strip("/") if "/products/" in raw else raw
    await state.update_data(lot=key[:255])
    async with sessions() as session:
        has_fazer = bool(await gc.get_api_key(session, message.from_user.id))
        has_ar = bool(await gc.get_api_key(session, message.from_user.id, "approute"))
    if has_ar and has_fazer:
        await state.set_state(AddMap.provider)
        await message.answer("У какого поставщика покупать?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("FazerCards", "gc:pv:fazer"), _btn("AppRoute", "gc:pv:approute")],
        ]))
        return
    if has_ar:
        await _ar_catalog(message, state, sessions, message.from_user.id)
        return
    await _fazer_catalog(message, state, sessions, message.from_user.id)


@router.callback_query(AddMap.provider, F.data.startswith("gc:pv:"))
async def add_provider_pick(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await cb.answer()
    if cb.data.endswith(":approute"):
        await _ar_catalog(cb.message, state, sessions, cb.from_user.id)
    else:
        await _fazer_catalog(cb.message, state, sessions, cb.from_user.id)


async def _ar_catalog(message: Message, state: FSMContext, sessions: SessionFactory, tg: int) -> None:
    await state.update_data(provider="approute")
    await state.set_state(AddMap.category)
    try:
        async with sessions() as session:
            api = await gc.make_approute(session, tg)
        async with api:
            products = await api.services()
    except FazerError as e:
        await message.answer(
            f"Не смог загрузить каталог AppRoute: <code>{html.escape(str(e))[:200]}</code>\n"
            "Проверь ключ («🔌 Проверить AppRoute») и начни заново."
        )
        await state.clear()
        return
    cats = [
        (str(p.get("id")), " · ".join(x for x in (str(p.get("name") or p.get("id")), p.get("countryCode") or "") if x))
        for p in products if p.get("id") and str(p.get("type") or "voucher") != "direct_topup"
    ]
    await state.update_data(cats=cats)
    await _ask_category(message, state, cats)


async def _fazer_catalog(message: Message, state: FSMContext, sessions: SessionFactory, tg: int) -> None:
    await state.update_data(provider="fazer")
    await state.set_state(AddMap.category)
    try:
        async with await _client(sessions, tg) as api:
            items = await api.categories()
    except FazerError as e:
        await message.answer(
            f"Не смог загрузить каталог FazerCards: <code>{html.escape(str(e))[:200]}</code>\n"
            "Проверь ключ («🔌 Проверить FazerCards») и начни заново."
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
    approute = (await state.get_data()).get("provider") == "approute"
    try:
        if approute:
            async with sessions() as session:
                api = await gc.make_approute(session, cb.from_user.id)
            async with api:
                product = await api.service(category_id)
            offers = [
                (str(i.get("id")),
                 f"{i.get('name') or i.get('nominal')} — ${i.get('price')}"
                 + (f" (в наличии {i.get('stock')})" if i.get("stock") is not None
                    else "" if i.get("available", True) else " (нет в наличии)"))
                for i in product.get("items") or [] if i.get("id") is not None
            ]
        else:
            async with await _client(sessions, cb.from_user.id) as api:
                data = await api.offers(category_id)
            offers = [
                (str(o.get("card_id")),
                 f"{o.get('name')} — ${o.get('price_usd')} (в наличии {o.get('stock')})")
                for o in data.get("offers") or [] if o.get("card_id") is not None
            ]
    except FazerError as e:
        await cb.message.answer(f"Не смог загрузить номиналы: <code>{html.escape(str(e))[:200]}</code>")
        return
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
            provider=data.get("provider") or "fazer",
            card_name=f"{data.get('category_name', '')} — {data.get('card_label', '').split(' — ')[0]}"[:128],
        ))
        await session.commit()
        seller = await get_or_create_seller(session, user)
    await reply.answer(
        f"✅ Привязано: «{html.escape(data['lot'][:60])}» → "
        f"{gc.PROVIDERS.get(data.get('provider') or 'fazer')}: {html.escape(data.get('category_name', ''))}, "
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


def _usd(value: float | None, exact: bool | None) -> str:
    if value is None:
        return "—"
    return f"{'' if exact else '≈'}${value:,.2f}".replace(",", " ")


@router.callback_query(F.data == "gc:orders")
async def orders(cb: CallbackQuery, sessions: SessionFactory) -> None:
    tg = cb.from_user.id
    async with sessions() as session:
        rows = list(await session.scalars(
            select(GiftcardOrder).where(GiftcardOrder.seller_tg_id == tg)
            .order_by(GiftcardOrder.id.desc()).limit(20)
        ))
        prices = dict((await session.execute(
            select(DealState.deal_id, DealState.price).where(
                DealState.seller_tg_id == tg, DealState.deal_id.in_([o.deal_id for o in rows])
            )
        )).all()) if rows else {}
        tz = timedelta(hours=await ft.get_tz(session, tg))
        all_done = list(await session.scalars(
            select(GiftcardOrder).where(GiftcardOrder.seller_tg_id == tg, GiftcardOrder.status == "DELIVERED")
        ))
        all_prices = dict((await session.execute(
            select(DealState.deal_id, DealState.price).where(
                DealState.seller_tg_id == tg, DealState.deal_id.in_([o.deal_id for o in all_done])
            )
        )).all()) if all_done else {}
    await cb.answer()
    if not rows:
        await cb.message.answer("Заказов Gift Card пока не было.")
        return
    lines = ["<b>📦 История заказов Gift Card</b>", ""]
    kb: list[list[InlineKeyboardButton]] = []
    for o in rows:
        when = (o.created_at + tz).strftime("%d.%m %H:%M") if o.created_at else "—"
        sale = prices.get(o.deal_id)
        lines.append(
            f"<b>{when}</b> · {html.escape(o.item_name[:40])}\n"
            f"   👤 {html.escape(o.buyer or '—')} · {STATUS_RU.get(o.status, o.status)}\n"
            f"   💰 продано: {f'{sale:g} ₽' if isinstance(sale, (int, float)) else '—'} · "
            f"🛒 куплено: {_usd(o.cost_usd, o.cost_exact)}"
            + (f" · {html.escape(o.provider_order_id)}" if o.provider_order_id else "")
        )
        if o.error and o.status != "DELIVERED":
            lines.append(f"   <i>{html.escape(o.error[:200])}</i>")
        if o.status in ("FAILED", "UNKNOWN") or (o.status == "NEEDS_CHECK" and o.codes_enc):
            kb.append([_btn(f"🔁 Повторить: {o.item_name[:25]}", f"gc:retry:{o.id}")])
    spent = sum(o.cost_usd or 0 for o in all_done)
    sold = sum(v or 0 for v in all_prices.values())
    approx = any(o.cost_usd is not None and not o.cost_exact for o in all_done)
    lines += [
        "",
        f"<b>Итого выдано:</b> {len(all_done)} шт · продано на {sold:g} ₽ · "
        f"куплено за {'≈' if approx else ''}${spent:,.2f}".replace(",", " "),
        "<i>≈ — цена номинала из каталога поставщика на момент выдачи (в ответе заказа суммы не было).</i>"
        if approx else "",
    ]
    await cb.message.answer("\n".join(lines).strip()[:4000],
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb) if kb else None)


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
    lines += ["", "Нажми — бот купит карту у поставщика привязки и отправит код покупателю в чат (≤30 с), "
              "затем отметит заказ выполненным."]
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("gc:give:"))
async def paid_give(cb: CallbackQuery, sessions: SessionFactory) -> None:
    deal_id = cb.data.split(":", 2)[2]
    async with sessions() as session:
        if not (await gc.get_api_key(session, cb.from_user.id)
                or await gc.get_api_key(session, cb.from_user.id, "approute")):
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
    ar_maps = [m for m in maps if gc.provider_of(m) == "approute"]
    maps = [m for m in maps if gc.provider_of(m) != "approute"]
    if ar_maps:
        try:
            async with sessions() as session:
                ar = await gc.make_approute(session, cb.from_user.id)
            async with ar:
                balance, cur = await ar.balance()
                lines.append(f"✅ AppRoute: ключ работает, доступно {html.escape(balance)} {html.escape(cur)}")
                for m in ar_maps:
                    try:
                        product = await ar.service(m.category_id)
                    except FazerError as e:
                        lines.append(f"❌ {html.escape(m.lot_key[:40])}: {html.escape(str(e))[:150]}")
                        continue
                    item = next((i for i in product.get("items") or [] if str(i.get("id")) == m.card_id), None)
                    if item is None:
                        lines.append(f"❌ {html.escape(m.lot_key[:40])}: номинала больше нет в AppRoute")
                        continue
                    stock = item.get("stock")
                    ok = item.get("available", True) and (stock is None or stock >= m.quantity)
                    lines.append(
                        f"{'✅' if ok else '⚠️'} {html.escape(m.lot_key[:40])}: AppRoute {html.escape(str(item.get('name') or item.get('nominal')))}, "
                        f"${html.escape(str(item.get('price')))} ×{m.quantity}"
                        + (f", в наличии {stock}" if stock is not None else "" if item.get("available", True) else ", нет в наличии")
                    )
        except FazerError as e:
            lines.append(f"❌ AppRoute: {html.escape(str(e))[:300]}")
    if not maps:
        lines += [] if ar_maps else ["Привязок нет — добавь «➕ Привязать лот»."]
        await cb.message.answer("\n".join(lines)[:4000])
        return
    try:
        async with await _client(sessions, cb.from_user.id) as api:
            balance, cur = await api.balance()
            lines.append(f"✅ FazerCards: ключ работает, баланс {html.escape(balance)} {html.escape(cur)}")
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
    await cb.message.answer("\n".join(lines)[:4000])


@router.message(StateFilter(AddMap, ShowOffers, SetKey), F.text.func(is_cancel))
@router.message(StateFilter(AddMap, ShowOffers, SetKey), Command("cancel"))
async def cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
