"""Меню автоматизации: включение функций, тексты, параметры, автовыдача, автоответчик."""

from __future__ import annotations

import hashlib
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select, update

from ..db import AutoReply, DealState, DeliveryItem, RelistRule, Seller, SessionFactory, Template
from ..crypto import TokenCipher
from ..keyboards import is_cancel, BTN_SETTINGS, cancel_kb, main_menu
from ..playerok import AuthRequired, PlayerokClient, PlayerokError
from ..plugins import PAID_PLUGINS
from ..plugins.access import has_access
from ..services import features as ft
from ..services.automation import relist_after_sale_overview, relist_candidates
from ..services.sellers import get_or_create_seller

router = Router(name="settings")


class EditText(StatesGroup):
    text = State()


class EditParam(StatesGroup):
    value = State()


class ItemRule(StatesGroup):
    name = State()


class AddStock(StatesGroup):
    lot = State()
    items = State()


class AddReply(StatesGroup):
    keyword = State()
    text = State()


class AddRelistRule(StatesGroup):
    pattern = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _status(flag: bool) -> str:
    return "🟢 Включено" if flag else "🔴 Выключено"


def _h(name: str) -> str:
    return hashlib.md5(name.lower().encode()).hexdigest()[:10]


# ===================== главное меню настроек =====================


async def settings_menu_kb(sessions: SessionFactory, tg_id: int) -> InlineKeyboardMarkup:
    rows = []
    async with sessions() as session:
        for f in ft.FEATURES:
            title = f.title
            if f.key in PAID_PLUGINS and not await has_access(session, tg_id, f.key):
                title += " 🔒"
            rows.append([_btn(title, f"f:{f.key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
@router.message(F.text == BTN_SETTINGS)
async def settings_menu(message: Message, sessions: SessionFactory) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    if not seller.is_connected:
        await message.answer(
            "Сначала подключи аккаунт Playerok: кнопка «Войти в Playerok».",
            reply_markup=main_menu(False),
        )
        return
    await message.answer(
        "⚙️ <b>Автоматизация</b>\nВыбери функцию:",
        reply_markup=await settings_menu_kb(sessions, message.from_user.id),
    )


@router.callback_query(F.data == "st")
async def settings_menu_cb(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await _show(
        cb, "⚙️ <b>Автоматизация</b>\nВыбери функцию:", await settings_menu_kb(sessions, cb.from_user.id)
    )


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)
    try:
        await cb.answer()
    except TelegramBadRequest:
        pass  # уже ответили (например, всплывающим текстом)


# ===================== экран функции =====================


async def render_feature(
    sessions: SessionFactory, seller: Seller, feature: ft.Feature
) -> tuple[str, InlineKeyboardMarkup]:
    if feature.special == "autodelivery":
        return await render_autodelivery(sessions, seller, feature)
    if feature.special == "autoresponder":
        return await render_autoresponder(sessions, seller, feature)
    if feature.special == "relist":
        return await render_relist(sessions, seller, feature)
    if feature.special == "stars":
        from ..plugins.stars.handlers import render_stars

        return await render_stars(sessions, seller, feature)

    tg = seller.tg_id
    async with sessions() as session:
        enabled = await ft.is_enabled(session, tg, feature)
        lines = [f"<b>{feature.title}</b>", "", feature.description, "", f"<b>Статус:</b> {_status(enabled)}"]
        rows: list[list[InlineKeyboardButton]] = [
            [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")]
        ]
        for t in feature.templates:
            text = await ft.get_template(session, tg, t.kind)
            lines += ["", f"<b>{t.label.capitalize()}:</b>", ft.quote(text)]
            rows.append([_btn(f"✏️ Изменить: {t.label}", f"f:{feature.key}:e:{t.kind}")])
            if t.per_item:
                rules = await ft.item_templates(session, tg, t.kind)
                lines += ["", "🎯 <b>Правила по лотам:</b>"]
                lines.append(
                    "\n".join(f"• {html.escape(r.item_name)}" for r in rules) or "<i>Не добавлены</i>"
                )
                rows.append([_btn(f"➕ {t.label.capitalize()} для лота", f"f:{feature.key}:i:{t.kind}")])
                rows += [[_btn(f"🗑 {r.item_name[:30]}", f"f:{feature.key}:x:{r.id}")] for r in rules]
        for p in feature.params:
            value = await ft.get_param(session, tg, p.key)
            rows.append([_btn(f"⏱ {p.label}: {value} {p.unit}", f"f:{feature.key}:p:{p.key}")])
        rows += await _toggle_rows(session, tg, feature)
    if feature.templates:
        lines += ["", ft.VARIABLES_HELP]
    if feature.note:
        lines += ["", feature.note]
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _toggle_rows(session, tg: int, feature: ft.Feature) -> list[list[InlineKeyboardButton]]:
    rows = []
    for key, label, default in feature.toggles:
        flag = await ft.get_flag(session, tg, key, default)
        rows.append([_btn(f"{'✅' if flag else '☑️'} {label}", f"f:{feature.key}:g:{key}")])
    return rows


async def _seller(sessions: SessionFactory, cb: CallbackQuery) -> Seller:
    async with sessions() as session:
        return await get_or_create_seller(session, cb.from_user)


async def _refresh(cb: CallbackQuery, sessions: SessionFactory, feature: ft.Feature) -> None:
    seller = await _seller(sessions, cb)
    text, kb = await render_feature(sessions, seller, feature)
    await _show(cb, text, kb)


@router.callback_query(F.data.startswith("f:"))
async def feature_cb(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    parts = cb.data.split(":")
    feature = ft.FEATURE_BY_KEY.get(parts[1])
    if feature is None:
        await cb.answer()
        return
    tg = cb.from_user.id
    action = parts[2] if len(parts) > 2 else ""
    arg = parts[3] if len(parts) > 3 else ""
    if feature.key in PAID_PLUGINS and action:
        async with sessions() as session:
            if not await has_access(session, tg, feature.key):
                await cb.answer("Платный плагин: доступ выдаёт владелец бота", show_alert=True)
                return

    if action == "t":
        async with sessions() as session:
            enabled = await ft.is_enabled(session, tg, feature)
            await ft.set_setting(session, tg, f"{feature.key}_enabled", "0" if enabled else "1")
    elif action == "g" and any(arg == k for k, _, _ in feature.toggles):
        async with sessions() as session:
            default = next(d for k, _, d in feature.toggles if k == arg)
            flag = await ft.get_flag(session, tg, arg, default)
            await ft.set_setting(session, tg, arg, "0" if flag else "1")
    elif action == "e" and arg in ft.TEMPLATE_KINDS:
        await state.set_state(EditText.text)
        await state.update_data(kind=arg, item_name="", feature=feature.key)
        await cb.answer()
        extra = f"\n{ft.STARS_VARIABLES_HELP}" if arg.startswith("stars_") else ""
        await cb.message.answer(
            f"Пришли новый текст ({ft.TEMPLATE_KINDS[arg].label}).\n\n{ft.VARIABLES_HELP}{extra}",
            reply_markup=cancel_kb(),
        )
        return
    elif action == "i" and arg in ft.TEMPLATE_KINDS:
        await state.set_state(ItemRule.name)
        await state.update_data(kind=arg, feature=feature.key)
        await cb.answer()
        await cb.message.answer(
            "Пришли точное название лота, для которого нужен отдельный текст:",
            reply_markup=cancel_kb(),
        )
        return
    elif action == "x" and arg.isdigit():
        async with sessions() as session:
            await session.execute(
                delete(Template).where(Template.id == int(arg), Template.seller_tg_id == tg)
            )
            await session.commit()
    elif action == "p" and arg in ft.PARAMS:
        p = ft.PARAMS[arg]
        await state.set_state(EditParam.value)
        await state.update_data(pkey=arg, feature=feature.key)
        await cb.answer()
        await cb.message.answer(
            f"{p.label}: пришли число от {p.minimum} до {p.maximum} ({p.unit}).",
            reply_markup=cancel_kb(),
        )
        return
    await _refresh(cb, sessions, feature)


# ===================== FSM: тексты, параметры, правила =====================


async def _finish(message: Message, state: FSMContext, sessions: SessionFactory, note: str) -> None:
    data = await state.get_data()
    await state.clear()
    feature = ft.FEATURE_BY_KEY[data["feature"]]
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(note, reply_markup=main_menu(seller.is_connected))
    text, kb = await render_feature(sessions, seller, feature)
    await message.answer(text, reply_markup=kb)


@router.message(StateFilter(EditText, EditParam, ItemRule, AddStock, AddReply, AddRelistRule), F.text.func(is_cancel))
@router.message(StateFilter(EditText, EditParam, ItemRule, AddStock, AddReply, AddRelistRule), Command("cancel"))
async def cancel_edit(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))


@router.message(EditText.text, F.text)
async def save_text(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    data = await state.get_data()
    async with sessions() as session:
        await ft.set_template(
            session, message.from_user.id, data["kind"], message.text.strip(), data["item_name"]
        )
    await _finish(message, state, sessions, "✅ Текст сохранён.")


@router.message(ItemRule.name, F.text)
async def item_rule_name(message: Message, state: FSMContext) -> None:
    await state.update_data(item_name=message.text.strip())
    await state.set_state(EditText.text)
    await message.answer(
        f"Теперь текст для лота «{html.escape(message.text.strip())}»:\n\n{ft.VARIABLES_HELP}"
    )


@router.message(EditParam.value, F.text)
async def save_param(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    data = await state.get_data()
    p = ft.PARAMS[data["pkey"]]
    try:
        value = int(message.text.strip())
    except ValueError:
        await message.answer("Нужно целое число. Попробуй ещё раз или нажми «Отмена».")
        return
    if not p.minimum <= value <= p.maximum:
        await message.answer(f"Число должно быть от {p.minimum} до {p.maximum}.")
        return
    async with sessions() as session:
        await ft.set_setting(session, message.from_user.id, p.key, str(value))
    await _finish(message, state, sessions, f"✅ {p.label}: {value} {p.unit}.")


# ===================== автовыдача =====================


async def render_autodelivery(
    sessions: SessionFactory, seller: Seller, feature: ft.Feature
) -> tuple[str, InlineKeyboardMarkup]:
    tg = seller.tg_id
    async with sessions() as session:
        enabled = await ft.is_enabled(session, tg, feature)
        confirm_after = await ft.get_flag(session, tg, "autodelivery_confirm", True)
        intro = await ft.get_template(session, tg, "delivery_msg")
        rows_db = await session.execute(
            select(
                func.max(DeliveryItem.item_name),
                func.count(DeliveryItem.id).filter(DeliveryItem.used_deal_id.is_(None)),
                func.count(DeliveryItem.id),
            )
            .where(DeliveryItem.seller_tg_id == tg)
            .group_by(DeliveryItem.item_key)
        )
        lots = [(name, int(free or 0), int(total)) for name, free, total in rows_db]
    lines = [
        f"<b>{feature.title}</b>",
        "",
        feature.description,
        "Лот находится по точному названию. Каждая строка запаса — один товар "
        "(ключ, аккаунт, инструкция), выдаётся по одному на заказ.",
        "",
        f"<b>Статус:</b> {_status(enabled)}",
        "<b>После выдачи:</b> "
        + ("отмечаю заказ выполненным и прошу покупателя подтвердить" if confirm_after else "заказ не трогаю"),
        "",
        "<b>Текст перед товаром:</b>",
        ft.quote(intro),
        "",
        "<b>Запас по лотам:</b>",
    ]
    if lots:
        lines += [f"• {html.escape(n)} — осталось {free} из {total}" for n, free, total in lots]
    else:
        lines.append("<i>Пока пусто</i>")
    lines += ["", "Нажми на лот, чтобы посмотреть остаток и управлять товарами."]
    rows = [
        [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")],
        [_btn(f"{'✅' if confirm_after else '☑️'} Отмечать заказ выполненным после выдачи", "ad:confirm")],
        [_btn("➕ Добавить товары", "ad:add")],
        [_btn("✏️ Текст перед товаром", f"f:{feature.key}:e:delivery_msg")],
        [_btn("✏️ Сообщение «подтвердите заказ»", "f:autoconfirm:e:autoconfirm_msg")],
    ]
    rows += [[_btn(f"📦 {n[:24]} — {free}/{total}", f"ad:lot:{_h(n)}")] for n, free, total in lots]
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _mask(content: str) -> str:
    """Короткий товар показываем целиком, длинный — с середины скрываем."""
    c = content.strip()
    if len(c) <= 40:
        return c
    return c[:30] + "…" + c[-6:]


async def _lot_name_by_hash(session, tg: int, h: str) -> tuple[str, str] | None:
    rows = await session.execute(
        select(DeliveryItem.item_name, DeliveryItem.item_key)
        .where(DeliveryItem.seller_tg_id == tg)
        .distinct()
    )
    for name, key in rows:
        if _h(key) == h:
            return name, key
    return None


async def render_lot_detail(
    sessions: SessionFactory, tg: int, h: str
) -> tuple[str, InlineKeyboardMarkup]:
    async with sessions() as session:
        found = await _lot_name_by_hash(session, tg, h)
        if found is None:
            return "Лот не найден или пуст.", InlineKeyboardMarkup(
                inline_keyboard=[[_btn("‹ Назад", "f:autodelivery")]]
            )
        name, key = found
        free = list(
            await session.scalars(
                select(DeliveryItem)
                .where(
                    DeliveryItem.seller_tg_id == tg,
                    DeliveryItem.item_key == key,
                    DeliveryItem.used_deal_id.is_(None),
                )
                .order_by(DeliveryItem.id)
            )
        )
        used = await session.scalar(
            select(func.count(DeliveryItem.id)).where(
                DeliveryItem.seller_tg_id == tg,
                DeliveryItem.item_key == key,
                DeliveryItem.used_deal_id.is_not(None),
            )
        )
    lines = [
        f"📦 <b>{html.escape(name)}</b>",
        "",
        f"<b>Свободно:</b> {len(free)}  ·  <b>Выдано:</b> {used or 0}",
        "",
        "<b>Товары в запасе:</b>",
    ]
    lines += [f"{i}. <code>{html.escape(_mask(item.content))}</code>" for i, item in enumerate(free, 1)]
    if not free:
        lines.append("<i>пусто — добавь новые</i>")
    rows = [
        [_btn("➕ Добавить к этому лоту", f"ad:addto:{h}")],
        [_btn("🗑 Очистить свободный запас", f"ad:clear:{h}")],
        [_btn("❌ Удалить лот из автовыдачи", f"ad:dellot:{h}")],
    ]
    rows += [[_btn(f"❌ Удалить №{i}", f"ad:rm:{item.id}")] for i, item in enumerate(free[:15], 1)]
    rows.append([_btn("‹ К автовыдаче", "f:autodelivery")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("ad:lot:"))
async def lot_detail(cb: CallbackQuery, sessions: SessionFactory) -> None:
    text, kb = await render_lot_detail(sessions, cb.from_user.id, cb.data.split(":")[2])
    await _show(cb, text, kb)


@router.callback_query(F.data.startswith("ad:addto:"))
async def stock_addto(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    h = cb.data.split(":")[2]
    async with sessions() as session:
        found = await _lot_name_by_hash(session, cb.from_user.id, h)
    if found is None:
        await cb.answer("Лот не найден", show_alert=True)
        return
    await state.set_state(AddStock.items)
    await state.update_data(feature="autodelivery", lot=found[0])
    await cb.answer()
    await cb.message.answer(
        f"Пришли товары для «{html.escape(found[0])}», по одному на строку.",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data.startswith("ad:rm:"))
async def stock_remove_one(cb: CallbackQuery, sessions: SessionFactory) -> None:
    item_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        item = await session.get(DeliveryItem, item_id)
        if item is None or item.seller_tg_id != cb.from_user.id or item.used_deal_id is not None:
            await cb.answer("Товар уже удалён или выдан", show_alert=True)
            return
        h = _h(item.item_key)
        await session.delete(item)
        await session.commit()
    text, kb = await render_lot_detail(sessions, cb.from_user.id, h)
    await _show(cb, text, kb)


@router.callback_query(F.data == "ad:add")
async def stock_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddStock.lot)
    await state.update_data(feature="autodelivery")
    await cb.answer()
    await cb.message.answer(
        "Пришли точное название лота (как на Playerok):", reply_markup=cancel_kb()
    )


@router.message(AddStock.lot, F.text)
async def stock_lot(message: Message, state: FSMContext) -> None:
    await state.update_data(lot=message.text.strip())
    await state.set_state(AddStock.items)
    await message.answer(
        "Теперь пришли товары, по одному на строку. Каждая строка уйдёт одному покупателю."
    )


@router.message(AddStock.items, F.text)
async def stock_items(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    lot = (await state.get_data())["lot"]
    units = [line.strip() for line in message.text.splitlines() if line.strip()]
    if not units:
        await message.answer("Пусто. Пришли хотя бы одну строку.")
        return
    key = lot.lower()
    async with sessions() as session:
        # Защита от дублей: не добавляем товар, который для этого лота уже есть
        # (хоть свободный, хоть уже выданный) — чтобы выданные коды не вернулись.
        existing = set(
            await session.scalars(
                select(DeliveryItem.content).where(
                    DeliveryItem.seller_tg_id == message.from_user.id,
                    DeliveryItem.item_key == key,
                )
            )
        )
        fresh, dupes, seen = [], 0, set()
        for u in units:
            if u in existing or u in seen:
                dupes += 1
                continue
            seen.add(u)
            fresh.append(DeliveryItem(seller_tg_id=message.from_user.id, item_name=lot, item_key=key, content=u))
        session.add_all(fresh)
        await session.commit()
    note = f"✅ Добавлено {len(fresh)} шт. для «{html.escape(lot)}»."
    if dupes:
        note += f"\nПропущено дублей (уже были в этом лоте): {dupes}."
    await _finish(message, state, sessions, note)


@router.callback_query(F.data.startswith("ad:clear:"))
async def stock_clear(cb: CallbackQuery, sessions: SessionFactory) -> None:
    h = cb.data.split(":")[2]
    async with sessions() as session:
        keys = await session.scalars(
            select(DeliveryItem.item_key).where(DeliveryItem.seller_tg_id == cb.from_user.id).distinct()
        )
        for key in keys:
            if _h(key) == h:
                await session.execute(
                    delete(DeliveryItem).where(
                        DeliveryItem.seller_tg_id == cb.from_user.id,
                        DeliveryItem.used_deal_id.is_(None),
                        DeliveryItem.item_key == key,
                    )
                )
        await session.commit()
    await _refresh(cb, sessions, ft.FEATURE_BY_KEY["autodelivery"])
    await cb.answer("Запас очищен")


@router.callback_query(F.data.startswith("ad:dellot:"))
async def lot_delete_ask(cb: CallbackQuery, sessions: SessionFactory) -> None:
    h = cb.data.split(":")[2]
    async with sessions() as session:
        found = await _lot_name_by_hash(session, cb.from_user.id, h)
    if found is None:
        await cb.answer("Лот уже удалён", show_alert=True)
        return
    text = (
        f"Удалить лот «{html.escape(found[0])}» из автовыдачи целиком?\n\n"
        "Удалятся все его товары — и свободные, и история выданных. "
        "Новые заказы на этот лот бот выдавать не будет."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [_btn("✅ Да, удалить", f"ad:dellotok:{h}")],
        [_btn("‹ Отмена", f"ad:lot:{h}")],
    ])
    await _show(cb, text, kb)


@router.callback_query(F.data.startswith("ad:dellotok:"))
async def lot_delete(cb: CallbackQuery, sessions: SessionFactory) -> None:
    h = cb.data.split(":")[2]
    async with sessions() as session:
        found = await _lot_name_by_hash(session, cb.from_user.id, h)
        if found is not None:
            await session.execute(
                delete(DeliveryItem).where(
                    DeliveryItem.seller_tg_id == cb.from_user.id, DeliveryItem.item_key == found[1]
                )
            )
            await session.commit()
    await cb.answer("Лот удалён из автовыдачи" if found else "Лот уже удалён")
    await _refresh(cb, sessions, ft.FEATURE_BY_KEY["autodelivery"])


# ===================== автоответчик =====================


async def render_autoresponder(
    sessions: SessionFactory, seller: Seller, feature: ft.Feature
) -> tuple[str, InlineKeyboardMarkup]:
    tg = seller.tg_id
    async with sessions() as session:
        enabled = await ft.is_enabled(session, tg, feature)
        rules = list(await session.scalars(select(AutoReply).where(AutoReply.seller_tg_id == tg)))
    lines = [
        f"<b>{feature.title}</b>",
        "",
        feature.description,
        "Если в сообщении покупателя встречается ключевое слово, бот отвечает заданным текстом. "
        "Регистр не важен.",
        "",
        f"<b>Статус:</b> {_status(enabled)}",
        "",
        "<b>Правила:</b>",
    ]
    if rules:
        for r in rules:
            lines.append(f"• <b>{html.escape(r.keyword)}</b> → {html.escape(r.text[:80])}")
    else:
        lines.append("<i>Не добавлены</i>")
    lines += ["", "В ответе работают <code>{Имя_Клиента}</code> и <code>{Аккаунт}</code>."]
    rows = [
        [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")],
        [_btn("➕ Добавить правило", "ar:add")],
    ]
    rows += [[_btn(f"🗑 {r.keyword[:30]}", f"ar:del:{r.id}")] for r in rules]
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "ar:add")
async def reply_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddReply.keyword)
    await state.update_data(feature="autoresponder")
    await cb.answer()
    await cb.message.answer("Пришли ключевое слово или фразу:", reply_markup=cancel_kb())


@router.message(AddReply.keyword, F.text)
async def reply_keyword(message: Message, state: FSMContext) -> None:
    await state.update_data(keyword=message.text.strip())
    await state.set_state(AddReply.text)
    await message.answer("Теперь текст ответа:")


@router.message(AddReply.text, F.text)
async def reply_text(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    keyword = (await state.get_data())["keyword"]
    async with sessions() as session:
        session.add(AutoReply(seller_tg_id=message.from_user.id, keyword=keyword, text=message.text.strip()))
        await session.commit()
    await _finish(message, state, sessions, "✅ Правило добавлено.")


@router.callback_query(F.data.startswith("ar:del:"))
async def reply_del(cb: CallbackQuery, sessions: SessionFactory) -> None:
    rule_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        await session.execute(
            delete(AutoReply).where(AutoReply.id == rule_id, AutoReply.seller_tg_id == cb.from_user.id)
        )
        await session.commit()
    await _refresh(cb, sessions, ft.FEATURE_BY_KEY["autoresponder"])


# ===================== автовыставление =====================


def _a(url: str | None, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>' if url else text


async def render_relist(
    sessions: SessionFactory, seller: Seller, feature: ft.Feature
) -> tuple[str, InlineKeyboardMarkup]:
    tg = seller.tg_id
    async with sessions() as session:
        enabled = await ft.is_enabled(session, tg, feature)
        all_lots = await ft.get_flag(session, tg, "relist_all", True)
        paid_ok = await ft.get_flag(session, tg, "relist_paid_allowed", False)
        interval = await ft.get_param(session, tg, "relist_interval_hours")
        rules = list(await session.scalars(select(RelistRule).where(RelistRule.seller_tg_id == tg)))
        toggle_rows = await _toggle_rows(session, tg, feature)
        sold = await relist_after_sale_overview(session, tg)
    lines = [
        f"<b>{feature.title}</b>",
        "",
        feature.description,
        "",
        f"<b>Статус:</b> {_status(enabled)}",
        f"<b>Режим:</b> {'все проданные лоты' if all_lots else 'только по правилам'}",
        f"<b>Интервал:</b> {interval} ч." if interval else "<b>Интервал:</b> сразу",
        f"<b>Платное восстановление:</b> {'🟢 разрешено' if paid_ok else '🔴 запрещено (только бесплатные статусы)'}",
        f"<b>Правил отбора:</b> {len(rules)}",
    ]
    if rules:
        lines += [""] + [f"• {html.escape(r.pattern)}" for r in rules]
    lines += ["", "<b>Лоты после продаж (48 ч):</b>"]
    if sold:
        lines += [
            f"• {_a(st.item_url, html.escape(st.item_name or 'Лот'))} — {html.escape(note)}"
            for st, note, _ in sold
        ]
    else:
        lines.append("пока продаж не было")
    lines += ["", feature.note]
    rows = [[_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")]]
    rows.append([_btn(f"⏱ Интервал: {interval} ч.", f"f:{feature.key}:p:relist_interval_hours")])
    rows += toggle_rows
    rows.append([_btn("➕ Добавить правило (слово/ссылка)", "rl:add")])
    rows += [[_btn(f"🗑 {r.pattern[:30]}", f"rl:del:{r.id}")] for r in rules]
    seen: set[str] = set()
    for st, _, manual in sold:
        if manual and st.item_id not in seen and len(f"rl:pub:{st.item_id}".encode()) <= 64:
            seen.add(st.item_id)
            rows.append([_btn(f"🔄 {(st.item_name or 'Лот')[:28]}", f"rl:pub:{st.item_id}")])
    rows.append([_btn("👁 Показать и восстановить вручную", "rl:preview")])
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "rl:add")
async def relist_rule_add(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddRelistRule.pattern)
    await state.update_data(feature="relist")
    await cb.answer()
    await cb.message.answer(
        "Пришли ключевое слово из названия лота или ссылку на лот:", reply_markup=cancel_kb()
    )


@router.message(AddRelistRule.pattern, F.text)
async def relist_rule_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    async with sessions() as session:
        session.add(RelistRule(seller_tg_id=message.from_user.id, pattern=message.text.strip()))
        await session.commit()
    await _finish(message, state, sessions, "✅ Правило добавлено.")


@router.callback_query(F.data.startswith("rl:del:"))
async def relist_rule_del(cb: CallbackQuery, sessions: SessionFactory) -> None:
    rule_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        await session.execute(
            delete(RelistRule).where(RelistRule.id == rule_id, RelistRule.seller_tg_id == cb.from_user.id)
        )
        await session.commit()
    await _refresh(cb, sessions, ft.FEATURE_BY_KEY["relist"])


async def _fetch_candidates(sessions: SessionFactory, seller, cipher: TokenCipher):
    """Возвращает (список лотов-кандидатов, текст ошибки или None)."""
    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            items = await client.my_items(seller.playerok_id or "", statuses=["APPROVED", "SOLD", "EXPIRED"])
    except AuthRequired as e:
        return [], f"Playerok не отдал лоты (похоже на проблему доступа): {e}"
    except PlayerokError as e:
        return [], str(e)
    async with sessions() as session:
        return await relist_candidates(session, seller.tg_id, items), None


@router.callback_query(F.data == "rl:preview")
async def relist_preview(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    seller = await _seller(sessions, cb)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Загружаю лоты…")
    candidates, error = await _fetch_candidates(sessions, seller, cipher)
    if error:
        await cb.message.answer(
            "ℹ️ Playerok не отдаёт боту общий список лотов "
            f"(<code>{html.escape(error)[:120]}</code>).\n\n"
            "Проданные лоты выставляются заново и без него — они перечислены на экране "
            "«Автовыставление»: жми «🔄» рядом с нужным лотом."
        )
        return
    if not candidates:
        await cb.message.answer("Сейчас под восстановление ничего не попадает.")
        return
    lines = ["<b>Под восстановление попадут:</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for i in candidates[:20]:
        paid = " 💸платно" if i.is_paid_relist else ""
        lines.append(f"• {html.escape(i.name)} — {i.status.lower()}{paid}")
        rows.append([_btn(f"♻️ {i.name[:28]}", f"rl:do:{i.id}")])
    if len(candidates) > 1:
        rows.insert(0, [_btn(f"♻️ Восстановить все ({len(candidates)})", "rl:all")])
    rows.append([_btn("‹ Назад", "f:relist")])
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


async def _restore(
    client: PlayerokClient, sessions: SessionFactory, tg: int, item_id: str, name: str,
    *, after_sale: bool = False,
) -> str:
    """after_sale — ID взят из сделки: настоящий лот ищем среди проданных по названию."""
    from ..db import ActionLog

    async with sessions() as session:
        allow_paid = await ft.get_flag(session, tg, "relist_paid_allowed", False)
        seller = await session.get(Seller, tg)
        known = await session.scalar(
            select(DealState).where(DealState.seller_tg_id == tg, DealState.item_id == item_id)
            .order_by(DealState.id.desc()).limit(1)
        )
    if known is not None and name == "Лот":
        name = known.item_name or name
    try:
        url = (known.item_url or "") if known is not None else ""
        slug = url.split("/products/", 1)[1] if "/products/" in url else None
        await client.publish_item(
            item_id, price=known.price if known is not None else None, allow_paid=allow_paid,
            slug=slug if slug and slug != item_id else None,
            sold_name=(known.item_name if known is not None else None) if after_sale else None,
            user_id=seller.playerok_id if seller is not None else None,
        )
    except (AuthRequired, PlayerokError) as e:
        return f"❌ {html.escape(name)}: {html.escape(str(e))[:600]}"
    async with sessions() as session:
        session.add(ActionLog(seller_tg_id=tg, kind="relist", target=item_id))
        # продажи с этим лотом считаем обработанными — автоматом второй раз не выставлять
        await session.execute(
            update(DealState)
            .where(DealState.seller_tg_id == tg, DealState.item_id == item_id)
            .values(relisted=True, relist_note="ok")
        )
        await session.commit()
    return f"✅ {html.escape(name)}"


@router.callback_query(F.data.startswith("rl:do:"))
async def relist_do(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    item_id = cb.data.split(":", 2)[2]
    seller = await _seller(sessions, cb)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Восстанавливаю…")
    candidates, error = await _fetch_candidates(sessions, seller, cipher)
    if error:
        await cb.message.answer(f"⚠️ {html.escape(error)}")
        return
    item = next((i for i in candidates if i.id == item_id), None)
    if item is None:
        await cb.message.answer("Этот лот уже не в списке на восстановление.")
        return
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        result = await _restore(client, sessions, seller.tg_id, item.id, item.name)
    await cb.message.answer(result)


@router.callback_query(F.data == "rl:all")
async def relist_all(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    seller = await _seller(sessions, cb)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Восстанавливаю все…")
    candidates, error = await _fetch_candidates(sessions, seller, cipher)
    if error:
        await cb.message.answer(f"⚠️ {html.escape(error)}")
        return
    if not candidates:
        await cb.message.answer("Восстанавливать нечего.")
        return
    results = []
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        for item in candidates[:30]:
            results.append(await _restore(client, sessions, seller.tg_id, item.id, item.name))
    await cb.message.answer("\n".join(results))


@router.callback_query(F.data.startswith("rl:pub:"))
async def relist_publish_now(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    """Кнопка «🔄 Выставить заново» под уведомлением о заказе: публикует лот по ID,
    без запроса списка лотов."""
    item_id = cb.data.split(":", 2)[2]
    seller = await _seller(sessions, cb)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Выставляю…")
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        result = await _restore(client, sessions, seller.tg_id, item_id, "Лот", after_sale=True)
    await cb.message.answer(result.replace("✅ ", "✅ Выставлен заново: ", 1))


@router.callback_query(F.data == "ad:confirm")
async def toggle_confirm_after_delivery(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        cur = await ft.get_flag(session, cb.from_user.id, "autodelivery_confirm", True)
        await ft.set_setting(session, cb.from_user.id, "autodelivery_confirm", "0" if cur else "1")
    await _refresh(cb, sessions, ft.FEATURE_BY_KEY["autodelivery"])
