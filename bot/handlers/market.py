"""«📦 Разделы закупки» для «Выгоды по рынку»: бренд → страна → номиналы и цены кнопками."""

from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..crypto import TokenCipher
from ..db import CostEntry, SessionFactory
from ..keyboards import cancel_kb, is_cancel, main_menu
from ..services import market_sections as ms
from ..services import pricing
from ..services.sellers import get_or_create_seller

router = Router(name="market")


class SectionInput(StatesGroup):
    add = State()
    price = State()
    lot = State()
    kw = State()
    country = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _rows(buttons: list[InlineKeyboardButton], per_row: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        await cb.message.answer(text, reply_markup=kb, disable_web_page_preview=True)


def _n(count: int) -> str:
    return f"{count} номинал" + ("" if count % 10 == 1 and count % 100 != 11 else
                                 "а" if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14) else "ов")


# ===================== экраны =====================


async def home_screen(sessions: SessionFactory, tg: int) -> tuple[str, InlineKeyboardMarkup]:
    async with sessions() as session:
        await ms.migrate_legacy(session, tg)
        secs = await ms.sections(session, tg)
        counts = await ms.counts(session, tg)
        act = await ms.active(session, tg)
    lines = [
        "📦 <b>Разделы закупки</b>",
        "",
        "Выбери бренд, потом страну — и добавь номиналы с ценой закупки в $. Они сохранятся, "
        "заново вводить не нужно. Отчёт и трекер считают по разделу со ★.",
    ]
    if secs:
        lines += ["", "<b>Твои разделы:</b>"]
        for s in secs:
            star = "★ " if act and act.id == s.id else "• "
            lines.append(f"{star}{html.escape(ms.section_title(s))} — {_n(counts.get(s.id, 0))}")
    per_brand: dict[str, int] = {}
    for s in secs:
        per_brand[s.brand] = per_brand.get(s.brand, 0) + 1
    brand_btns = [
        _btn(f"{title} ({per_brand[key]})" if key in per_brand else title, f"mk:b:{key}")
        for key, title in ms.BRANDS.items()
    ]
    rows = _rows(brand_btns, 2)
    rows += [[_btn(f"{'★ ' if act and act.id == s.id else ''}{ms.section_title(s)} ({counts.get(s.id, 0)})",
                   f"mk:s:{s.id}")] for s in secs[:20]]
    rows.append([_btn("‹ К расчёту", "dp:nom")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def brand_screen(sessions: SessionFactory, tg: int, brand: str) -> tuple[str, InlineKeyboardMarkup]:
    async with sessions() as session:
        secs = {s.country: s for s in await ms.sections(session, tg, brand)}
        counts = await ms.counts(session, tg)
    title = ms.BRANDS.get(brand, brand)
    keys = list(ms.COUNTRIES)
    if brand == "roblox":
        keys = ["any"] + [k for k in keys if k != "any"]
    btns = []
    for key in keys:
        label = ms.country_title(key)
        if key in secs:
            label = f"✅ {label} ({counts.get(secs[key].id, 0)})"
        btns.append(_btn(label, f"mk:c:{brand}:{key}"))
    rows = _rows(btns, 2)
    rows += [[_btn(f"✅ {ms.country_title(c)} ({counts.get(s.id, 0)})", f"mk:s:{s.id}")]
             for c, s in secs.items() if c not in ms.COUNTRIES]
    rows.append([_btn("✏️ Своя страна", f"mk:bc:{brand}")])
    rows.append([_btn("‹ Разделы", "mk:home")])
    return f"{title} — выбери страну:", InlineKeyboardMarkup(inline_keyboard=rows)


async def section_screen(sessions: SessionFactory, tg: int, section_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    from ..plugins.access import is_admin

    async with sessions() as session:
        sec = await ms.get_section(session, tg, section_id)
        if sec is None:
            return None
        items = await ms.entries(session, sec.id)
        act = await ms.active(session, tg)
    is_active = act is not None and act.id == sec.id
    words = ms.kw_list(sec)
    lines = [
        f"<b>{html.escape(ms.section_title(sec))}</b>" + (" — ★ по нему считаю" if is_active else ""),
        "",
        "<b>Номиналы и закупка:</b>",
    ]
    lines += [f"• {e.nominal} → ${e.cost:g}" for e in items] or ["пока нет — «➕ Добавить номиналы»"]
    lines += [
        "",
        "<b>Лот для сравнения:</b> " + (html.escape(sec.lot_ref[:80]) if sec.lot_ref
                                        else "не задан — «🔗 Лот для сравнения»"),
        "<b>Страна в названиях конкурентов:</b> " + (html.escape(", ".join(words)) if words else "не проверяю"),
    ]
    admin = is_admin(tg)
    if admin:
        lines.append("<b>FazerCards:</b> " + (html.escape(sec.fazer_name or sec.fazer_cat) + " (главнее ручной)"
                                              if sec.fazer_cat else "не выбран"))
    rows = [[_btn("➕ Добавить номиналы", f"mk:add:{sec.id}")]]
    rows += _rows([_btn(f"✏️ {e.nominal} · ${e.cost:g}", f"mk:e:{e.id}") for e in items[:30]], 3)
    rows.append([_btn("🔗 Лот для сравнения", f"mk:lot:{sec.id}"), _btn("🌍 Страна", f"mk:kw:{sec.id}")])
    if admin:
        rows.append([_btn(f"🎁 FazerCards: {(sec.fazer_name or sec.fazer_cat)[:18]}" if sec.fazer_cat
                          else "🎁 Цены с FazerCards", f"mk:fz:{sec.id}")]
                    + ([_btn("✖️", f"mk:fzx:{sec.id}")] if sec.fazer_cat else []))
    if not is_active:
        rows.append([_btn("★ Считать по этому разделу", f"mk:use:{sec.id}")])
    rows.append([_btn("▶️ Посчитать", f"mk:run:{sec.id}")])
    rows.append([_btn("🗑 Удалить раздел", f"mk:del:{sec.id}"), _btn("‹ Разделы", "mk:home")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def send_section(message: Message, sessions: SessionFactory, tg: int, section_id: int) -> None:
    screen = await section_screen(sessions, tg, section_id)
    if screen:
        await message.answer(screen[0], reply_markup=screen[1], disable_web_page_preview=True)


async def _clear_input(state: FSMContext) -> None:
    cur = await state.get_state() or ""
    if cur.startswith("SectionInput") or cur.startswith("NominalInput"):
        await state.clear()


# ===================== навигация =====================


@router.callback_query(F.data == "mk:home")
async def home(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await _clear_input(state)
    await cb.answer()
    await _show(cb, *await home_screen(sessions, cb.from_user.id))


@router.callback_query(F.data.startswith("mk:b:"))
async def brand(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await _clear_input(state)
    key = cb.data.split(":", 2)[2]
    if key not in ms.BRANDS:
        await cb.answer("Нет такого раздела", show_alert=True)
        return
    await cb.answer()
    await _show(cb, *await brand_screen(sessions, cb.from_user.id, key))


@router.callback_query(F.data.startswith("mk:c:"))
async def country(cb: CallbackQuery, sessions: SessionFactory) -> None:
    _, _, brand_key, country_key = cb.data.split(":", 3)
    if brand_key not in ms.BRANDS or country_key not in ms.COUNTRIES:
        await cb.answer("Нет такой страны", show_alert=True)
        return
    async with sessions() as session:
        sec = await ms.get_or_create(session, cb.from_user.id, brand_key, country_key)
    await cb.answer()
    await _show(cb, *await section_screen(sessions, cb.from_user.id, sec.id))


@router.callback_query(F.data.startswith("mk:s:"))
async def section(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await _clear_input(state)
    screen = await section_screen(sessions, cb.from_user.id, int(cb.data.split(":")[2]))
    if screen is None:
        await cb.answer("Раздел удалён", show_alert=True)
        return
    await cb.answer()
    await _show(cb, *screen)


@router.callback_query(F.data.startswith("mk:bc:"))
async def custom_country_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SectionInput.country)
    await state.update_data(brand=cb.data.split(":", 2)[2])
    await cb.answer()
    await cb.message.answer("Напиши название страны или региона, например <code>Канада</code>:",
                            reply_markup=cancel_kb())


@router.message(SectionInput.country, F.text, ~F.text.func(is_cancel))
async def custom_country_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    brand_key = (await state.get_data()).get("brand", "")
    name = " ".join(message.text.split())[:40]
    if brand_key not in ms.BRANDS or not name:
        await message.answer("Не понял название — напиши ещё раз или нажми «Отмена».")
        return
    await state.clear()
    async with sessions() as session:
        sec = await ms.get_or_create(session, message.from_user.id, brand_key, name)
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Раздел создан.", reply_markup=main_menu(seller.is_connected))
    await send_section(message, sessions, message.from_user.id, sec.id)


# ===================== номиналы =====================


@router.callback_query(F.data.startswith("mk:add:"))
async def add_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SectionInput.add)
    await state.update_data(section_id=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer(
        "Пришли номинал и закупку в $ через пробел. Можно сразу несколько строк:\n"
        "<code>5 4.6\n10 9.1\n25 22.5</code>\n"
        "Если номинал уже есть — цена заменится.",
        reply_markup=cancel_kb(),
    )


@router.message(SectionInput.add, F.text, ~F.text.func(is_cancel))
async def add_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    costs = pricing.parse_costs(message.text)
    if not costs:
        await message.answer("Не понял. Формат: <code>10 9.1</code> — номинал и цена в $, по строке.")
        return
    section_id = (await state.get_data()).get("section_id")
    await state.clear()
    async with sessions() as session:
        sec = await ms.get_section(session, message.from_user.id, int(section_id or 0))
        if sec is not None:
            await ms.upsert_costs(session, sec, costs)
        seller = await get_or_create_seller(session, message.from_user)
    if sec is None:
        await message.answer("Раздел удалён.", reply_markup=main_menu(seller.is_connected))
        return
    await message.answer(f"✅ Сохранил: {_n(len(costs))}.", reply_markup=main_menu(seller.is_connected))
    await send_section(message, sessions, message.from_user.id, sec.id)


async def _entry(sessions: SessionFactory, tg: int, entry_id: int) -> CostEntry | None:
    async with sessions() as session:
        e = await session.get(CostEntry, entry_id)
    return e if e is not None and e.seller_tg_id == tg else None


@router.callback_query(F.data.startswith("mk:e:"))
async def entry_ask(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    e = await _entry(sessions, cb.from_user.id, int(cb.data.split(":")[2]))
    if e is None:
        await cb.answer("Номинал уже удалён", show_alert=True)
        return
    await state.set_state(SectionInput.price)
    await state.update_data(entry_id=e.id, section_id=e.section_id)
    await cb.answer()
    await cb.message.answer(
        f"Номинал <b>{e.nominal}</b>: закупка сейчас ${e.cost:g}.\nПришли новую цену в $ или удали номинал:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_btn("🗑 Удалить номинал", f"mk:ed:{e.id}"), _btn("‹ Назад", f"mk:s:{e.section_id}")],
        ]),
    )


@router.message(SectionInput.price, F.text, ~F.text.func(is_cancel))
async def entry_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    try:
        value = float(message.text.replace("$", "").replace(",", ".").strip())
    except ValueError:
        value = -1
    if value <= 0:
        await message.answer("Нужно число больше 0, например <code>9.1</code>.")
        return
    data = await state.get_data()
    await state.clear()
    async with sessions() as session:
        e = await session.get(CostEntry, int(data.get("entry_id") or 0))
        if e is not None and e.seller_tg_id == message.from_user.id:
            e.cost = value
            await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer(f"✅ Цена: ${value:g}", reply_markup=main_menu(seller.is_connected))
    await send_section(message, sessions, message.from_user.id, int(data.get("section_id") or 0))


@router.callback_query(F.data.startswith("mk:ed:"))
async def entry_delete(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    await _clear_input(state)
    e = await _entry(sessions, cb.from_user.id, int(cb.data.split(":")[2]))
    if e is None:
        await cb.answer("Уже удалён", show_alert=True)
        return
    async with sessions() as session:
        obj = await session.get(CostEntry, e.id)
        await session.delete(obj)
        await session.commit()
    await cb.answer(f"Номинал {e.nominal} удалён")
    screen = await section_screen(sessions, cb.from_user.id, e.section_id)
    if screen:
        await _show(cb, *screen)


# ===================== настройки раздела =====================


@router.callback_query(F.data.startswith("mk:lot:"))
async def lot_ask(cb: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SectionInput.lot)
    await state.update_data(section_id=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer(
        "Пришли ссылку на любой лот этой категории и способа получения на Playerok "
        "(например, свой или чужой лот «Apple 10$ США»). По нему бот поймёт, где искать конкурентов:",
        reply_markup=cancel_kb(),
    )


@router.message(SectionInput.lot, F.text, ~F.text.func(is_cancel))
async def lot_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    if "/products/" not in message.text:
        await message.answer("Нужна ссылка вида https://playerok.com/products/…")
        return
    section_id = int((await state.get_data()).get("section_id") or 0)
    await state.clear()
    async with sessions() as session:
        sec = await ms.get_section(session, message.from_user.id, section_id)
        if sec is not None:
            sec.lot_ref = message.text.strip()[:500]
            await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Запомнил.", reply_markup=main_menu(seller.is_connected))
    await send_section(message, sessions, message.from_user.id, section_id)


@router.callback_query(F.data.startswith("mk:kw:"))
async def kw_ask(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    section_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        sec = await ms.get_section(session, cb.from_user.id, section_id)
    if sec is None:
        await cb.answer("Раздел удалён", show_alert=True)
        return
    await state.set_state(SectionInput.kw)
    await state.update_data(section_id=section_id)
    await cb.answer()
    cur = ", ".join(ms.kw_list(sec))
    await cb.message.answer(
        "Если в категории Playerok лоты разных стран, бот берёт только те, где в названии есть "
        "слово страны. Пришли слова через запятую, например <code>usa, сша</code>.\n"
        "Отправь <code>-</code>, чтобы не проверять страну."
        + (f"\n\nСейчас: <code>{html.escape(cur)}</code>" if cur else ""),
        reply_markup=cancel_kb(),
    )


@router.message(SectionInput.kw, F.text, ~F.text.func(is_cancel))
async def kw_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    section_id = int((await state.get_data()).get("section_id") or 0)
    words = [] if message.text.strip() in ("-", "—", "нет") else [
        w.strip().lower() for w in message.text.replace(";", ",").split(",") if w.strip()
    ]
    await state.clear()
    async with sessions() as session:
        sec = await ms.get_section(session, message.from_user.id, section_id)
        if sec is not None:
            sec.kw = ", ".join(words)[:255]
            await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Сохранил." if words else "✅ Страну не проверяю.", reply_markup=main_menu(seller.is_connected))
    await send_section(message, sessions, message.from_user.id, section_id)


@router.callback_query(F.data.startswith("mk:fz:"))
async def fazer_ask(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    """Категория FazerCards для раздела — тот же поиск, что в «🎁 Закупка с FazerCards»."""
    from ..plugins.access import is_admin
    from ..plugins.giftcard import service as gc
    from .pricing import NominalInput

    if not is_admin(cb.from_user.id):
        await cb.answer("Только для владельца", show_alert=True)
        return
    async with sessions() as session:
        key = await gc.get_api_key(session, cb.from_user.id)
    if not key:
        await cb.answer("Сначала введи API-ключ FazerCards: /giftcard → 🔑", show_alert=True)
        return
    await state.set_state(NominalInput.fazer)
    await state.update_data(section_id=int(cb.data.split(":")[2]))
    await cb.answer()
    await cb.message.answer(
        "С какой картой FazerCards сравнивать? Напиши часть названия, например <code>apple us</code>:",
        reply_markup=cancel_kb(),
    )


@router.callback_query(F.data.startswith("mk:fzx:"))
async def fazer_off(cb: CallbackQuery, sessions: SessionFactory) -> None:
    section_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        sec = await ms.get_section(session, cb.from_user.id, section_id)
        if sec is not None:
            sec.fazer_cat, sec.fazer_name = "", ""
            await session.commit()
    await cb.answer("FazerCards отключён — считаю по ценам вручную")
    screen = await section_screen(sessions, cb.from_user.id, section_id)
    if screen:
        await _show(cb, *screen)


@router.callback_query(F.data.startswith("mk:use:"))
async def use(cb: CallbackQuery, sessions: SessionFactory) -> None:
    section_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        sec = await ms.get_section(session, cb.from_user.id, section_id)
        if sec is not None:
            await ms.set_active(session, cb.from_user.id, sec)
    if sec is None:
        await cb.answer("Раздел удалён", show_alert=True)
        return
    await cb.answer("★ Отчёт и трекер считают по этому разделу")
    await _show(cb, *await section_screen(sessions, cb.from_user.id, section_id))


@router.callback_query(F.data.startswith("mk:run:"))
async def run(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    from .pricing import _nominal

    section_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        sec = await ms.get_section(session, cb.from_user.id, section_id)
        if sec is not None:
            await ms.set_active(session, cb.from_user.id, sec)
            has_costs = bool(await ms.entries(session, sec.id)) or bool(sec.fazer_cat)
    if sec is None:
        await cb.answer("Раздел удалён", show_alert=True)
        return
    if not sec.lot_ref:
        await cb.answer("Сначала «🔗 Лот для сравнения» — ссылка на лот этой категории", show_alert=True)
        return
    if not has_costs:
        await cb.answer("Сначала «➕ Добавить номиналы»", show_alert=True)
        return
    await cb.answer("Считаю…")
    await _nominal(cb.message, sessions, cipher, cb.from_user)


@router.callback_query(F.data.startswith("mk:del:"))
async def delete_ask(cb: CallbackQuery, sessions: SessionFactory) -> None:
    section_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        sec = await ms.get_section(session, cb.from_user.id, section_id)
    if sec is None:
        await cb.answer("Уже удалён", show_alert=True)
        return
    await cb.answer()
    await _show(cb, f"Удалить раздел <b>{html.escape(ms.section_title(sec))}</b> со всеми номиналами?",
                InlineKeyboardMarkup(inline_keyboard=[
                    [_btn("🗑 Да, удалить", f"mk:delok:{sec.id}"), _btn("Отмена", f"mk:s:{sec.id}")],
                ]))


@router.callback_query(F.data.startswith("mk:delok:"))
async def delete_ok(cb: CallbackQuery, sessions: SessionFactory) -> None:
    async with sessions() as session:
        sec = await ms.get_section(session, cb.from_user.id, int(cb.data.split(":")[2]))
        if sec is not None:
            await ms.delete_section(session, sec)
    await cb.answer("Удалено")
    await _show(cb, *await home_screen(sessions, cb.from_user.id))


@router.message(StateFilter(SectionInput), F.text.func(is_cancel))
@router.message(StateFilter(SectionInput), Command("cancel"))
async def cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
