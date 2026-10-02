"""Экран «⭐ Звёзды через Fragment»: cookies, кошелёк, правила лотов, заказы."""

from __future__ import annotations

import html
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import delete, func, select

from ...crypto import TokenCipher
from ...db import Seller, SessionFactory, StarsOrder, StarsRule
from ...keyboards import cancel_kb, is_cancel, main_menu
from ...playerok import PlayerokClient
from ...services import features as ft
from ...services.sellers import get_or_create_seller
from ..access import has_access, locked_screen
from . import service as st
from .fragment import FragmentClient, FragmentError, parse_cookies
from .ton import TonWallet, TonWalletError

router = Router(name="stars")
log = logging.getLogger(__name__)


async def _allowed(cb: CallbackQuery, sessions: SessionFactory) -> bool:
    async with sessions() as session:
        ok = await has_access(session, cb.from_user.id, st.PLUGIN_KEY)
    if not ok:
        await cb.answer("Платный плагин: доступ выдаёт владелец бота", show_alert=True)
    return ok

FEATURE = ft.FEATURE_BY_KEY["stars"]
STATUS_RU = {
    "awaiting_username": "⏳ жду @username",
    "processing": "🔄 покупаю",
    "done": "✅ выдано",
    "failed": "❌ ошибка",
}


class SetCookies(StatesGroup):
    text = State()


class SetSeed(StatesGroup):
    text = State()


class AddStarsRule(StatesGroup):
    pattern = State()
    stars = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


async def _delete_quietly(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        pass


# ===================== экран =====================


async def render_stars(
    sessions: SessionFactory, seller: Seller, feature: ft.Feature
) -> tuple[str, InlineKeyboardMarkup]:
    tg = seller.tg_id
    async with sessions() as session:
        if not await has_access(session, tg, st.PLUGIN_KEY):
            return await locked_screen(session, feature)
        enabled = await ft.is_enabled(session, tg, feature)
        has_cookies = bool(await ft.get_setting(session, tg, "stars_cookies_enc", ""))
        has_seed = bool(await ft.get_setting(session, tg, "stars_seed_enc", ""))
        address = await ft.get_setting(session, tg, "stars_wallet_address", "")
        version = await ft.get_setting(session, tg, "stars_wallet_version", "v5r1")
        min_usdt = await ft.get_param(session, tg, "stars_min_usdt")
        autoparse = await ft.get_flag(session, tg, "stars_autoparse", True)
        autoconfirm = await ft.get_flag(session, tg, "stars_autoconfirm", True)
        rules = list(await session.scalars(select(StarsRule).where(StarsRule.seller_tg_id == tg)))
        counts = dict(
            (
                await session.execute(
                    select(StarsOrder.status, func.count(StarsOrder.id))
                    .where(StarsOrder.seller_tg_id == tg)
                    .group_by(StarsOrder.status)
                )
            ).all()
        )
        templates = {t.kind: await ft.get_template(session, tg, t.kind) for t in feature.templates}

    lines = [
        f"<b>{feature.title}</b>",
        "",
        feature.description,
        "",
        f"<b>Статус:</b> {'🟢 Включено' if enabled else '🔴 Выключено'}",
        f"<b>Fragment:</b> {'🟢 cookies заданы' if has_cookies else '🔴 cookies не заданы'}",
        f"<b>Кошелёк:</b> {'🟢 ' + html.escape(address or 'подключён') if has_seed else '🔴 не подключён'}"
        + (f" ({version})" if has_seed else ""),
        f"<b>Число звёзд:</b> {'из названия лота + правила' if autoparse else 'только по правилам'}",
        "",
        "<b>Правила по лотам:</b>",
    ]
    lines += [f"• «{html.escape(r.pattern)}» → {r.stars} ⭐" for r in rules] or ["<i>Не добавлены</i>"]
    done, failed, waiting = counts.get("done", 0), counts.get("failed", 0), counts.get("awaiting_username", 0)
    lines += [
        "",
        f"<b>Заказы:</b> выдано {done}, ошибок {failed}, ждут @username {waiting}",
        "",
        "<b>Тексты:</b>",
    ]
    for t in feature.templates:
        lines.append(f"<i>{t.label}:</i> {html.escape(templates[t.kind][:80])}")
    lines += [
        "",
        "<b>Как подключить</b>",
        "1. Войди на fragment.com через Telegram и там же подключи кошелёк бота (Connect TON).",
        "2. Скопируй cookies сайта fragment.com и пришли боту кнопкой ниже.",
        "3. Создай в Tonkeeper отдельный кошелёк, пополни USDT (TON) и чуть TON на комиссии, "
        "пришли его seed-фразу кнопкой ниже (сообщение удалится).",
        "4. Назови лоты так, чтобы в названии было число звёзд («500 звёзд»), или добавь правило.",
        "",
        "⚠️ Seed-фраза хранится в базе в зашифрованном виде. Держи на этом кошельке только рабочий запас.",
    ]
    rows = [
        [_btn("🔴 Отключить" if enabled else "🟢 Включить", f"f:{feature.key}:t")],
        [_btn("🍪 Cookies Fragment", "sr:cookies"), _btn("👛 Seed кошелька", "sr:seed")],
        [_btn("🔎 Проверить Fragment и баланс", "sr:check")],
        [_btn(f"Версия кошелька: {version}", "sr:ver")],
        [_btn("➕ Правило лота", "sr:rule"), _btn("📋 Заказы", "sr:orders")],
        [_btn(f"{'✅' if autoparse else '☑️'} Число звёзд из названия", f"f:{feature.key}:g:stars_autoparse")],
        [_btn(f"{'✅' if autoconfirm else '☑️'} Подтверждать заказ после выдачи", f"f:{feature.key}:g:stars_autoconfirm")],
        [_btn(f"⏱ Предупреждать, если USDT < {min_usdt}", f"f:{feature.key}:p:stars_min_usdt")],
    ]
    rows += [[_btn(f"✏️ {t.label}", f"f:{feature.key}:e:{t.kind}")] for t in feature.templates]
    rows += [[_btn(f"🗑 {r.pattern[:25]} → {r.stars}", f"sr:rdel:{r.id}")] for r in rules]
    rows.append([_btn("‹ Назад", "st")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_screen(target: Message | CallbackQuery, sessions: SessionFactory, user) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
    text, kb = await render_stars(sessions, seller, FEATURE)
    msg = target.message if isinstance(target, CallbackQuery) else target
    if isinstance(target, CallbackQuery):
        try:
            await msg.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await msg.answer(text, reply_markup=kb)
        await target.answer()
    else:
        await msg.answer(text, reply_markup=kb)


# ===================== cookies / seed =====================


@router.callback_query(F.data == "sr:cookies")
async def ask_cookies(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    if not await _allowed(cb, sessions):
        return
    await state.set_state(SetCookies.text)
    await cb.answer()
    await cb.message.answer(
        "Пришли cookies сайта fragment.com одним сообщением.\n\n"
        "Как взять: открой fragment.com в браузере, где ты залогинен → DevTools → "
        "Application/Storage → Cookies → скопируй строки (нужны <code>stel_ssid</code>, "
        "<code>stel_token</code>, <code>stel_dt</code> и прочие <code>stel_*</code>). "
        "Подойдёт и экспорт из расширения Cookie-Editor (JSON), и строка вида "
        "<code>stel_ssid=...; stel_token=...</code>.\n\n"
        "Сообщение с cookies бот удалит сразу после сохранения.",
        reply_markup=cancel_kb(),
    )


@router.message(SetCookies.text, F.text)
async def save_cookies(
    message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher
) -> None:
    cookies = parse_cookies(message.text)
    await _delete_quietly(message)
    stel = [k for k in cookies if k.startswith("stel_")]
    if not stel:
        await message.answer(
            "Не вижу cookies Fragment (ожидал имена вида <code>stel_*</code>). "
            "Пришли ещё раз или нажми «Отмена»."
        )
        return
    async with sessions() as session:
        await ft.set_setting(session, message.from_user.id, "stars_cookies_enc", cipher.encrypt(message.text))
    await state.clear()
    try:
        async with FragmentClient(cookies) as fr:
            await fr.check_session()
        note = f"✅ Cookies сохранены ({len(stel)} шт.), сессия Fragment активна."
    except FragmentError as e:
        note = f"Cookies сохранены ({len(stel)} шт.), но проверка не прошла:\n<code>{html.escape(str(e))}</code>"
    await message.answer(note, reply_markup=main_menu(True))
    await _show_screen(message, sessions, message.from_user)


@router.callback_query(F.data == "sr:seed")
async def ask_seed(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    if not await _allowed(cb, sessions):
        return
    await state.set_state(SetSeed.text)
    await cb.answer()
    await cb.message.answer(
        "Пришли seed-фразу кошелька (24 слова через пробел). Это должен быть "
        "<b>отдельный</b> кошелёк только для бота.\n\n"
        "Сообщение с фразой бот удалит сразу после сохранения.",
        reply_markup=cancel_kb(),
    )


@router.message(SetSeed.text, F.text)
async def save_seed(
    message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher
) -> None:
    raw = message.text
    await _delete_quietly(message)
    try:
        words = TonWallet.validate_mnemonic(raw)
    except TonWalletError as e:
        await message.answer(f"{html.escape(str(e))}\nПришли ещё раз или нажми «Отмена».")
        return
    tg = message.from_user.id
    async with sessions() as session:
        await ft.set_setting(session, tg, "stars_seed_enc", cipher.encrypt(" ".join(words)))
        version = await ft.get_setting(session, tg, "stars_wallet_version", "v5r1")
    await state.clear()
    wallet = TonWallet(" ".join(words), version=version)
    try:
        address = await wallet.address()
        b = await wallet.balances()
        async with sessions() as session:
            await ft.set_setting(session, tg, "stars_wallet_address", address)
        note = (
            f"✅ Кошелёк подключён.\nАдрес: <code>{address}</code>\n"
            f"Баланс: {b.usdt:.2f} USDT, {b.ton:.3f} TON.\n\n"
            "Если адрес не совпадает с Tonkeeper — поменяй версию кошелька на экране звёзд."
        )
    except TonWalletError as e:
        note = f"Seed сохранён, но проверить кошелёк не удалось:\n<code>{html.escape(str(e))}</code>"
    finally:
        await wallet.aclose()
    await message.answer(note, reply_markup=main_menu(True))
    await _show_screen(message, sessions, message.from_user)


@router.callback_query(F.data == "sr:ver")
async def toggle_version(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    if not await _allowed(cb, sessions):
        return
    tg = cb.from_user.id
    async with sessions() as session:
        cur = await ft.get_setting(session, tg, "stars_wallet_version", "v5r1")
        new = "v4r2" if cur == "v5r1" else "v5r1"
        await ft.set_setting(session, tg, "stars_wallet_version", new)
        seller = await get_or_create_seller(session, cb.from_user)
        wallet = await st.get_wallet(session, seller, cipher)
    if wallet is not None:
        try:
            address = await wallet.address()
            async with sessions() as session:
                await ft.set_setting(session, tg, "stars_wallet_address", address)
        except TonWalletError:
            pass
        finally:
            await wallet.aclose()
    await _show_screen(cb, sessions, cb.from_user)


@router.callback_query(F.data == "sr:check")
async def check_all(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    if not await _allowed(cb, sessions):
        return
    await cb.answer("Проверяю…")
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
        cookies = await st.get_cookies(session, seller, cipher)
        wallet = await st.get_wallet(session, seller, cipher)
    lines = []
    if cookies:
        try:
            async with FragmentClient(cookies) as fr:
                await fr.check_session()
            lines.append("🟢 Fragment: сессия активна")
        except FragmentError as e:
            lines.append(f"🔴 Fragment: {html.escape(str(e))}")
    else:
        lines.append("🔴 Fragment: cookies не заданы")
    if wallet is not None:
        try:
            address = await wallet.address()
            b = await wallet.balances()
            lines.append(f"🟢 Кошелёк <code>{address}</code>\n   {b.usdt:.2f} USDT, {b.ton:.3f} TON")
            if b.ton < 0.2:
                lines.append("   ⚠️ Мало TON на комиссии, пополни хотя бы до 0.5 TON")
        except TonWalletError as e:
            lines.append(f"🔴 Кошелёк: {html.escape(str(e))}")
        finally:
            await wallet.aclose()
    else:
        lines.append("🔴 Кошелёк: не подключён")
    await cb.message.answer("\n".join(lines))


# ===================== правила лотов =====================


@router.callback_query(F.data == "sr:rule")
async def rule_add(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    if not await _allowed(cb, sessions):
        return
    await state.set_state(AddStarsRule.pattern)
    await cb.answer()
    await cb.message.answer(
        "Пришли слово или часть названия лота, по которому бот поймёт, что это звёзды "
        "(например «500 Stars» или «звёзды 1000»):",
        reply_markup=cancel_kb(),
    )


@router.message(AddStarsRule.pattern, F.text)
async def rule_pattern(message: Message, state: FSMContext) -> None:
    await state.update_data(pattern=message.text.strip())
    await state.set_state(AddStarsRule.stars)
    await message.answer("Сколько звёзд выдавать за такой лот? Число от 50:")


@router.message(AddStarsRule.stars, F.text)
async def rule_stars(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    try:
        qty = int(message.text.strip().replace(" ", ""))
    except ValueError:
        await message.answer("Нужно число. Попробуй ещё раз или нажми «Отмена».")
        return
    if not st.MIN_STARS <= qty <= st.MAX_STARS:
        await message.answer(f"Fragment продаёт от {st.MIN_STARS} до {st.MAX_STARS} звёзд.")
        return
    pattern = (await state.get_data())["pattern"]
    async with sessions() as session:
        session.add(StarsRule(seller_tg_id=message.from_user.id, pattern=pattern, stars=qty))
        await session.commit()
    await state.clear()
    await message.answer(f"✅ Правило: «{html.escape(pattern)}» → {qty} ⭐", reply_markup=main_menu(True))
    await _show_screen(message, sessions, message.from_user)


@router.callback_query(F.data.startswith("sr:rdel:"))
async def rule_del(cb: CallbackQuery, sessions: SessionFactory) -> None:
    if not await _allowed(cb, sessions):
        return
    rule_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        await session.execute(
            delete(StarsRule).where(StarsRule.id == rule_id, StarsRule.seller_tg_id == cb.from_user.id)
        )
        await session.commit()
    await _show_screen(cb, sessions, cb.from_user)


# ===================== заказы =====================


@router.callback_query(F.data == "sr:orders")
async def orders(cb: CallbackQuery, sessions: SessionFactory) -> None:
    if not await _allowed(cb, sessions):
        return
    async with sessions() as session:
        rows = list(
            await session.scalars(
                select(StarsOrder)
                .where(StarsOrder.seller_tg_id == cb.from_user.id)
                .order_by(StarsOrder.id.desc())
                .limit(15)
            )
        )
    if not rows:
        await cb.answer("Заказов звёзд пока не было", show_alert=True)
        return
    lines = ["⭐ <b>Последние заказы звёзд</b>", ""]
    kb: list[list[InlineKeyboardButton]] = []
    for o in rows:
        who = f"@{o.username}" if o.username else "—"
        lines.append(
            f"• {o.stars} ⭐ → {html.escape(who)} · {html.escape(o.buyer or '')} · "
            f"{STATUS_RU.get(o.status, o.status)}"
            + (f"\n   <code>{html.escape((o.error or '')[:120])}</code>" if o.status == "failed" else "")
        )
        if o.status == "failed" and o.username:
            kb.append([_btn(f"🔁 Повторить {o.stars}⭐ → @{o.username[:20]}", f"sr:retry:{o.id}")])
    kb.append([_btn("‹ К настройкам звёзд", "f:stars")])
    await cb.answer()
    await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.callback_query(F.data.startswith("sr:retry:"))
async def retry_order(cb: CallbackQuery, bot: Bot, sessions: SessionFactory, cipher: TokenCipher) -> None:
    if not await _allowed(cb, sessions):
        return
    order_id = int(cb.data.split(":")[2])
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
        order = await session.get(StarsOrder, order_id)
        if order is None or order.seller_tg_id != seller.tg_id or not seller.is_connected:
            await cb.answer("Заказ не найден", show_alert=True)
            return
        await cb.answer("Повторяю…")
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            await st.retry(bot, session, seller, client, cipher, order)
        await session.commit()


# ===================== отмена FSM =====================


@router.message(StateFilter(SetCookies, SetSeed, AddStarsRule), F.text.func(is_cancel))
@router.message(StateFilter(SetCookies, SetSeed, AddStarsRule), Command("cancel"))
async def cancel(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))
