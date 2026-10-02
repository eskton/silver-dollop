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
from sqlalchemy import delete, func, select

from ..db import AutoReply, DeliveryItem, RelistRule, Seller, SessionFactory, Template
from ..crypto import TokenCipher
from ..keyboards import is_cancel, BTN_SETTINGS, cancel_kb, main_menu
from ..playerok import AuthRequired, PlayerokClient, PlayerokError
from ..services import features as ft
from ..services.automation import relist_candidates
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


def settings_menu_kb() -> InlineKeyboardMarkup:
    rows = [[_btn(f.title, f"f:{f.key}")] for f in ft.FEATURES]
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
    await message.answer("⚙️ <b>Автоматизация</b>\nВыбери функцию:", reply_markup=settings_menu_kb())


@router.callback_query(F.data == "st")
async def settings_menu_cb(cb: CallbackQuery) -> None:
    await _show(cb, "⚙️ <b>Автоматизация</b>\nВыбери функцию:", settings_menu_kb())


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


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
        await cb.message.answer(
            f"Пришли новый текст ({ft.TEMPLATE_KINDS[arg].label}).\n\n{ft.VARIABLES_HELP}",
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
        intro = await ft.get_template(session, tg, "delivery_msg")
        rows_db = await session.execute(
            select(
                DeliveryItem.item_name,
                func.sum(DeliveryItem.used_deal_id.is_(None)),
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
    rows = [
        [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")],
        [_btn("➕ Добавить товары", "ad:add")],
        [_btn("✏️ Текст перед товаром", f"f:{feature.key}:e:delivery_msg")],
    ]
    rows += [[_btn(f"🗑 Очистить: {n[:28]}", f"ad:clear:{_h(n)}")] for n, _, _ in lots]
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


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
    async with sessions() as session:
        session.add_all(
            DeliveryItem(
                seller_tg_id=message.from_user.id, item_name=lot, item_key=lot.lower(), content=u
            )
            for u in units
        )
        await session.commit()
    await _finish(message, state, sessions, f"✅ Добавлено {len(units)} шт. для «{html.escape(lot)}».")


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
    lines += ["", feature.note]
    rows = [[_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")]]
    rows.append([_btn(f"⏱ Интервал: {interval} ч.", f"f:{feature.key}:p:relist_interval_hours")])
    rows += toggle_rows
    rows.append([_btn("➕ Добавить правило (слово/ссылка)", "rl:add")])
    rows += [[_btn(f"🗑 {r.pattern[:30]}", f"rl:del:{r.id}")] for r in rules]
    rows.append([_btn("👁 Показать, что попадёт под восстановление", "rl:preview")])
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


@router.callback_query(F.data == "rl:preview")
async def relist_preview(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    seller = await _seller(sessions, cb)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            items = await client.my_items(seller.playerok_id or "")
    except AuthRequired:
        await cb.answer("Сессия истекла, войди заново", show_alert=True)
        return
    except PlayerokError as e:
        await cb.answer(f"Ошибка Playerok: {e}"[:200], show_alert=True)
        return
    async with sessions() as session:
        candidates = await relist_candidates(session, seller.tg_id, items)
    await cb.answer()
    if not candidates:
        await cb.message.answer("Сейчас под восстановление ничего не попадает.")
        return
    lines = [f"• {html.escape(i.name)} — {i.status.lower()}" + (" (платно)" if i.is_paid_relist else "") for i in candidates[:50]]
    await cb.message.answer("Под восстановление попадут:\n" + "\n".join(lines))
