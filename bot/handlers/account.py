"""Профиль продавца: аккаунт, уведомления, чаты и заказы, клиенты, часовой пояс."""

import html
from collections import defaultdict
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select

from ..crypto import TokenCipher
from ..db import DealState, SessionFactory
from ..keyboards import BTN_ACCOUNT, BTN_NOTIFY, BTN_PROFILE, main_menu
from ..playerok import AuthRequired, PlayerokClient, PlayerokError
from ..services import features as ft
from ..services import notifications as nt
from ..services.analytics import SALE_STATUSES, money
from ..services.sellers import disconnect_seller, get_or_create_seller

router = Router(name="account")

STATUS_RU = {
    "PAID": "💰 оплачен",
    "SENT": "📦 выдан",
    "CONFIRMED": "✅ подтверждён",
    "COMPLETED": "✅ завершён",
}
TZ_CHOICES = (2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 0)


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


BACK = [_btn("‹ В профиль", "pf")]


def _tz_label(offset: int) -> str:
    sign = "+" if offset >= 0 else "−"
    name = " (МСК)" if offset == 3 else ""
    return f"UTC{sign}{abs(offset)}{name}"


async def _show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup) -> None:
    try:
        await cb.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except TelegramBadRequest:
        pass  # текст не изменился
    await cb.answer()


# ===================== главный экран профиля =====================


async def _profile(sessions: SessionFactory, user) -> tuple[str, InlineKeyboardMarkup] | None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
        tz = await ft.get_tz(session, seller.tg_id)
    if not seller.is_connected:
        return None
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"<b>Playerok:</b> {html.escape(seller.playerok_username or '—')}\n"
        f"<b>Почта:</b> {html.escape(seller.email or '—')}\n"
        f"<b>Часовой пояс:</b> {_tz_label(tz)}\n\n"
        "Выберите нужный раздел:"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [_btn("🔔 Уведомления", "pf:notify")],
            [_btn("💬 Чаты и заказы", "pf:orders")],
            [_btn("💲 Изменить цену лотов", "dp:cut")],
            [_btn("📣 Мои клиенты", "pf:clients")],
            [_btn("🕒 Часовой пояс", "pf:tz")],
            [_btn("✅ Прочитать все сообщения", "pf:readall")],
            [_btn("🔄 Проверить сессию", "account:check"), _btn("🚪 Выйти", "account:logout")],
        ]
    )
    return text, kb


@router.message(Command("profile", "account"))
@router.message(F.text.in_({BTN_PROFILE, BTN_ACCOUNT}))
async def show_profile(message: Message, sessions: SessionFactory) -> None:
    result = await _profile(sessions, message.from_user)
    if result is None:
        await message.answer(
            "Аккаунт Playerok не подключён. Нажми «Войти в Playerok».",
            reply_markup=main_menu(False),
        )
        return
    await message.answer(result[0], reply_markup=result[1])


@router.callback_query(F.data == "pf")
async def profile_cb(cb: CallbackQuery, sessions: SessionFactory) -> None:
    result = await _profile(sessions, cb.from_user)
    if result is None:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await _show(cb, *result)


# ===================== уведомления =====================


async def _notify_screen(sessions: SessionFactory, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    async with sessions() as session:
        for kind in nt.KINDS:
            on, sound = await nt.get_prefs(session, tg_id, kind.key)
            rows.append(
                [
                    _btn(f"{'🔔' if on else '🔕'} {kind.title}", f"n:{kind.key}:on"),
                    _btn("🔊 Со звуком" if sound else "🤫 Без звука", f"n:{kind.key}:sound"),
                ]
            )
    rows.append(BACK)
    text = (
        "🔔 <b>Уведомления</b>\n\n"
        "Слева включаете или выключаете тип уведомления (🔔 — приходит, 🔕 — нет), "
        "справа выбираете режим: «Со звуком» или «Без звука».\n\n"
        "О том, что сессия Playerok истекла, бот сообщит в любом случае."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("notify"))
@router.message(F.text == BTN_NOTIFY)
async def notify_cmd(message: Message, sessions: SessionFactory) -> None:
    text, kb = await _notify_screen(sessions, message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data == "pf:notify")
async def notify_cb(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await _show(cb, *await _notify_screen(sessions, cb.from_user.id))


@router.callback_query(F.data.startswith("n:"))
async def notify_toggle(cb: CallbackQuery, sessions: SessionFactory) -> None:
    _, kind, what = (cb.data.split(":") + ["", ""])[:3]
    if kind in nt.KIND_BY_KEY and what in ("on", "sound"):
        async with sessions() as session:
            await nt.toggle(session, cb.from_user.id, kind, what)
    await _show(cb, *await _notify_screen(sessions, cb.from_user.id))


# ===================== чаты и заказы =====================


@router.callback_query(F.data == "pf:orders")
async def orders_cb(cb: CallbackQuery, sessions: SessionFactory) -> None:
    tg = cb.from_user.id
    async with sessions() as session:
        tz = await ft.get_tz(session, tg)
        deals = list(
            await session.scalars(
                select(DealState)
                .where(DealState.seller_tg_id == tg)
                .order_by(DealState.created_at.desc(), DealState.id.desc())
                .limit(50)
            )
        )
    active = [d for d in deals if (d.status or "").upper() in ("PAID", "SENT")]
    recent = [d for d in deals if d not in active][:10]

    def line(d: DealState) -> str:
        when = d.created_at or d.first_seen_at
        when_s = (when + timedelta(hours=tz)).strftime("%d.%m %H:%M") if when else "—"
        status = STATUS_RU.get((d.status or "").upper(), (d.status or "—").lower())
        price = money(d.price) if d.price is not None else "—"
        return (
            f"• {when_s} · {html.escape((d.item_name or 'лот')[:30])} · {price} · "
            f"{html.escape(d.buyer or 'покупатель')} · {status}"
        )

    lines = ["💬 <b>Чаты и заказы</b>", ""]
    lines.append(f"<b>Активные ({len(active)}):</b>")
    lines += [line(d) for d in active[:15]] or ["<i>нет</i>"]
    lines += ["", "<b>Последние:</b>"]
    lines += [line(d) for d in recent] or ["<i>пока нет</i>"]
    if active:
        lines += ["", "Кнопки ниже — ответить покупателю по активному заказу."]

    rows = [
        [_btn(f"💬 {(d.buyer or 'покупатель')[:18]} — {(d.item_name or 'лот')[:20]}", f"reply:{d.chat_id}")]
        for d in active[:8]
        if d.chat_id
    ]
    rows.append(BACK)
    await _show(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


# ===================== мои клиенты =====================


@router.callback_query(F.data == "pf:clients")
async def clients_cb(cb: CallbackQuery, sessions: SessionFactory) -> None:
    tg = cb.from_user.id
    async with sessions() as session:
        tz = await ft.get_tz(session, tg)
        deals = list(await session.scalars(select(DealState).where(DealState.seller_tg_id == tg)))
    stats: dict[str, list] = defaultdict(lambda: [0, 0.0, None])  # заказов, сумма, последний
    for d in deals:
        if (d.status or "").upper() not in SALE_STATUSES or not d.buyer:
            continue
        s = stats[d.buyer]
        s[0] += 1
        s[1] += d.price or 0.0
        when = d.created_at or d.first_seen_at
        if when and (s[2] is None or when > s[2]):
            s[2] = when

    lines = ["📣 <b>Мои клиенты</b>", ""]
    if not stats:
        lines.append("Пока нет покупателей. Список появится после первых продаж.")
    else:
        repeat = sum(1 for s in stats.values() if s[0] > 1)
        lines += [
            f"Всего клиентов: <b>{len(stats)}</b>",
            f"Вернулись повторно: <b>{repeat}</b> ({repeat * 100 // len(stats)}%)",
            "",
            "🏆 <b>Топ по сумме покупок:</b>",
        ]
        top = sorted(stats.items(), key=lambda kv: (kv[1][1], kv[1][0]), reverse=True)[:15]
        for i, (buyer, (count, total, last)) in enumerate(top, 1):
            last_s = (last + timedelta(hours=tz)).strftime("%d.%m.%Y") if last else "—"
            lines.append(
                f"{i}. <b>{html.escape(buyer)}</b> — {count} зак. · {money(total)} · последний {last_s}"
            )
    await _show(cb, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=[BACK]))


# ===================== часовой пояс =====================


async def _tz_screen(sessions: SessionFactory, tg_id: int) -> tuple[str, InlineKeyboardMarkup]:
    async with sessions() as session:
        tz = await ft.get_tz(session, tg_id)
    now_local = datetime.utcnow() + timedelta(hours=tz)
    rows, row = [], []
    for offset in TZ_CHOICES:
        mark = "✅ " if offset == tz else ""
        row.append(_btn(f"{mark}{_tz_label(offset)}", f"tz:{offset}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(BACK)
    text = (
        "🕒 <b>Часовой пояс</b>\n\n"
        f"Сейчас: <b>{_tz_label(tz)}</b>, у вас {now_local:%H:%M}.\n"
        "Используется в аналитике (что считать «сегодня») и в датах заказов."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data == "pf:tz")
async def tz_cb(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await _show(cb, *await _tz_screen(sessions, cb.from_user.id))


@router.callback_query(F.data.startswith("tz:"))
async def tz_set(cb: CallbackQuery, sessions: SessionFactory) -> None:
    try:
        offset = int(cb.data.split(":", 1)[1])
    except ValueError:
        offset = 3
    if offset in TZ_CHOICES:
        async with sessions() as session:
            await ft.set_setting(session, cb.from_user.id, "tz", str(offset))
    await _show(cb, *await _tz_screen(sessions, cb.from_user.id))


# ===================== прочитать все сообщения =====================


@router.callback_query(F.data == "pf:readall")
async def read_all(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    await cb.answer("Отмечаю прочитанными…")
    done = failed = 0
    try:
        async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
            for chat in await client.chats(seller.playerok_id or "", limit=50):
                if chat.unread <= 0:
                    continue
                try:
                    await client.mark_chat_read(chat.id)
                    done += 1
                except AuthRequired:
                    raise
                except PlayerokError:
                    failed += 1
    except AuthRequired:
        await disconnect_seller(sessions, seller.tg_id)
        await cb.message.answer("⚠️ Сессия истекла, войди заново.", reply_markup=main_menu(False))
        return
    except PlayerokError as e:
        await cb.message.answer(f"Не получилось: {html.escape(str(e))}")
        return
    if not done and not failed:
        text = "✅ Непрочитанных сообщений нет."
    else:
        text = f"✅ Отмечено прочитанными чатов: {done}."
        if failed:
            text += f"\nНе удалось: {failed}."
    await cb.message.answer(text)


# ===================== сессия и выход =====================


@router.callback_query(F.data == "account:check")
async def check_session(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, cb.from_user)
    if not seller.is_connected:
        await cb.answer("Аккаунт не подключён", show_alert=True)
        return
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        try:
            viewer = await client.viewer()
        except AuthRequired:
            await disconnect_seller(sessions, seller.tg_id)
            await cb.answer()
            await cb.message.answer(
                "⚠️ Сессия истекла. Нажми «Войти в Playerok», чтобы подключить заново.",
                reply_markup=main_menu(False),
            )
            return
        except PlayerokError as e:
            await cb.answer(f"Ошибка Playerok: {e}"[:200], show_alert=True)
            return
    balance = f", баланс {viewer.balance:g} ₽" if isinstance(viewer.balance, (int, float)) else ""
    await cb.answer(f"Сессия активна: {viewer.username}{balance}", show_alert=True)


@router.callback_query(F.data == "account:logout")
async def logout(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await disconnect_seller(sessions, cb.from_user.id)
    await cb.answer()
    await cb.message.answer(
        "Аккаунт Playerok отключён, токен удалён из базы.", reply_markup=main_menu(False)
    )
