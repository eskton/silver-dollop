"""Кнопки под уведомлением о сообщении/заказе (как в Easy Sell):
✉️ Ответ, 📋 Шаблоны, 💬 Весь чат, ✅ Подтвердить, ↩️ Возврат."""

import html
import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from ..crypto import TokenCipher
from ..db import DealState, QuickReply, Seller, SessionFactory
from ..keyboards import is_cancel, cancel_kb, main_menu
from ..logs import tag
from ..playerok import AuthRequired, Deal, PlayerokClient, PlayerokError
from ..services import features as ft
from ..services.sellers import disconnect_seller, get_or_create_seller

log = logging.getLogger(__name__)
router = Router(name="chat")

MAX_QUICK = 20
DONE = ("CONFIRMED", "COMPLETED")
PROBLEM = ("PROBLEM", "DISPUTE", "ROLLBACK", "ROLLED_BACK", "REFUND")


class Reply(StatesGroup):
    text = State()


class QuickAdd(StatesGroup):
    text = State()


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


@router.callback_query(F.data.startswith("reply:"))
async def ask_reply(cb: CallbackQuery, state: FSMContext) -> None:
    chat_id = cb.data.split(":", 1)[1]
    await state.set_state(Reply.text)
    await state.update_data(chat_id=chat_id)
    await cb.answer()
    await cb.message.answer("Напиши ответ покупателю:", reply_markup=cancel_kb())


@router.message(StateFilter(Reply, QuickAdd), F.text.func(is_cancel))
@router.message(StateFilter(Reply, QuickAdd), Command("cancel"))
async def cancel_reply(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    await state.clear()
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("Отменено.", reply_markup=main_menu(seller.is_connected))


@router.message(Reply.text, F.text)
async def send_reply(
    message: Message, state: FSMContext, sessions: SessionFactory, cipher: TokenCipher
) -> None:
    chat_id = (await state.get_data())["chat_id"]
    async with sessions() as session:
        seller = await get_or_create_seller(session, message.from_user)
    if not seller.is_connected:
        await state.clear()
        await message.answer("Аккаунт Playerok не подключён.", reply_markup=main_menu(False))
        return

    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        try:
            await client.send_message(chat_id, message.text)
        except AuthRequired:
            await state.clear()
            await disconnect_seller(sessions, seller.tg_id)
            await message.answer(
                "⚠️ Сессия истекла, сообщение не отправлено. Войди заново.",
                reply_markup=main_menu(False),
            )
            return
        except PlayerokError as e:
            await message.answer(
                f"Не удалось отправить: {html.escape(str(e))}\nПопробуй ещё раз или нажми «Отмена»."
            )
            return

    await state.clear()
    await message.answer("✅ Отправлено.", reply_markup=main_menu(True))


# ===================== общее =====================


async def _connected(sessions: SessionFactory, user) -> Seller | None:
    async with sessions() as session:
        seller = await get_or_create_seller(session, user)
    return seller if seller.is_connected else None


async def _call(cb: CallbackQuery, sessions: SessionFactory, seller: Seller, fn) -> tuple[bool, object]:
    """Запрос к Playerok от имени продавца; ошибки — понятным текстом. (успех, результат)."""
    try:
        return True, await fn()
    except AuthRequired:
        await disconnect_seller(sessions, seller.tg_id)
        await cb.message.answer("⚠️ Сессия Playerok истекла. Войди заново.", reply_markup=main_menu(False))
    except PlayerokError as e:
        await cb.message.answer(f"⚠️ Playerok: <code>{html.escape(str(e))[:400]}</code>")
    return False, None


def _as_deal(s: DealState) -> Deal:
    return Deal(
        id=s.deal_id, status=s.status or "", item_id=s.item_id or "", item_slug="",
        item_name=s.item_name or "товар", price=s.price, buyer_id="",
        buyer_username=s.buyer or "покупатель", chat_id=s.chat_id, created_at="",
    )


async def _deal_for(sessions: SessionFactory, tg: int, ref: str) -> DealState | None:
    """ref = d<id заказа> или c<id чата> (тогда — активный заказ этого чата, иначе последний)."""
    async with sessions() as session:
        if ref.startswith("d"):
            return await session.scalar(
                select(DealState).where(DealState.seller_tg_id == tg, DealState.deal_id == ref[1:])
            )
        rows = list(await session.scalars(
            select(DealState)
            .where(DealState.seller_tg_id == tg, DealState.chat_id == ref[1:])
            .order_by(func.coalesce(DealState.created_at, DealState.first_seen_at).desc(), DealState.id.desc())
        ))
    for s in rows:
        if (s.status or "").upper() in ("PAID", "SENT"):
            return s
    return rows[0] if rows else None


def _deal_line(s: DealState) -> str:
    price = f" · {s.price:g} ₽" if isinstance(s.price, (int, float)) else ""
    return f"«{html.escape(s.item_name or 'товар')}»{price} · покупатель {html.escape(s.buyer or '—')}"


# ===================== ✅ Подтвердить / ↩️ Возврат =====================


def _blocked(s: DealState, refund: bool) -> str | None:
    """Почему с заказом нельзя это сделать (статус по последнему опросу)."""
    status = (s.status or "").upper()
    if status in DONE:
        return "Покупатель уже подтвердил этот заказ."
    if any(m in status for m in PROBLEM):
        return f"Заказ уже в статусе {status} — смотри на сайте."
    if not refund and status == "SENT":
        return "Заказ уже отмечен выполненным — ждём подтверждения покупателя."
    return None


@router.callback_query(F.data.startswith("cm:ok:"))
async def confirm_ask(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await _ask(cb, sessions, cb.data[len("cm:ok:"):], refund=False)


@router.callback_query(F.data.startswith("cm:rf:"))
async def refund_ask(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await _ask(cb, sessions, cb.data[len("cm:rf:"):], refund=True)


async def _ask(cb: CallbackQuery, sessions: SessionFactory, ref: str, *, refund: bool) -> None:
    if await _connected(sessions, cb.from_user) is None:
        await cb.answer("Аккаунт Playerok не подключён.", show_alert=True)
        return
    s = await _deal_for(sessions, cb.from_user.id, ref)
    if s is None:
        await cb.answer("Не нашёл заказ в этом чате. Сделай это на сайте Playerok.", show_alert=True)
        return
    why = _blocked(s, refund)
    if why:
        await cb.answer(why, show_alert=True)
        return
    if refund:
        text = (f"↩️ <b>Вернуть деньги покупателю?</b>\n\n{_deal_line(s)}\n\n"
                "Заказ отменится, деньги вернутся покупателю. Отменить возврат будет нельзя.")
        yes = _btn("↩️ Да, вернуть", f"cm:rfy:d{s.deal_id}")
    else:
        text = (f"✅ <b>Отметить заказ выполненным?</b>\n\n{_deal_line(s)}\n\n"
                "Делай это, только если товар уже выдан.")
        yes = _btn("✅ Да, выполнен", f"cm:oky:d{s.deal_id}")
    await cb.answer()
    await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[yes, _btn("Отмена", "cm:no")]]
    ))


@router.callback_query(F.data == "cm:no")
async def ask_cancel(cb: CallbackQuery) -> None:
    await cb.answer("Отменено")
    try:
        await cb.message.edit_text("Отменено.")
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("cm:oky:"))
async def confirm_do(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    await _do(cb, sessions, cipher, cb.data[len("cm:oky:"):], refund=False)


@router.callback_query(F.data.startswith("cm:rfy:"))
async def refund_do(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    await _do(cb, sessions, cipher, cb.data[len("cm:rfy:"):], refund=True)


async def _do(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher, ref: str, *, refund: bool) -> None:
    seller = await _connected(sessions, cb.from_user)
    s = await _deal_for(sessions, cb.from_user.id, ref) if seller else None
    if seller is None or s is None:
        await cb.answer("Заказ не найден.", show_alert=True)
        return
    why = _blocked(s, refund)
    if why:
        await cb.answer(why, show_alert=True)
        return
    await cb.answer("Отправляю в Playerok…")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)  # чтобы не нажать дважды
    except TelegramBadRequest:
        pass
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        fn = client.refund_deal if refund else client.confirm_deal
        ok, _ = await _call(cb, sessions, seller, lambda: fn(s.deal_id))
    if not ok:
        return
    log.info("%s %s: сделка %s", tag(seller.tg_id), "возврат" if refund else "подтверждено вручную", s.deal_id)
    done = "↩️ Возврат оформлен" if refund else "✅ Заказ отмечен выполненным"
    try:
        await cb.message.edit_text(f"{done}: {_deal_line(s)}")
    except TelegramBadRequest:
        await cb.message.answer(f"{done}: {_deal_line(s)}")


# ===================== 📋 Шаблоны =====================


async def _quick_list(sessions: SessionFactory, tg: int) -> list[QuickReply]:
    async with sessions() as session:
        return list(await session.scalars(
            select(QuickReply).where(QuickReply.seller_tg_id == tg).order_by(QuickReply.id)
        ))


async def _quick_screen(sessions: SessionFactory, tg: int, chat_id: str) -> tuple[str, InlineKeyboardMarkup]:
    items = await _quick_list(sessions, tg)
    rows = [[_btn(f"📨 {q.title}", f"cm:ts:{q.id}:{chat_id}")] for q in items]
    add = [_btn("➕ Добавить шаблон", f"cm:tadd:{chat_id}")]
    if items:
        add.append(_btn("🗑 Удалить", f"cm:tdl:{chat_id}"))
    rows.append(add)
    if items:
        text = "📋 <b>Шаблоны ответов</b>\n\nНажми шаблон — бот сразу отправит его покупателю в этот чат."
    else:
        text = ("📋 <b>Шаблоны ответов</b>\n\nШаблонов пока нет. Добавь частые ответы "
                "(«Сейчас выдам», «Код отправил» и т.п.) — потом они отправляются одной кнопкой.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("cm:tpl:"))
async def quick_menu(cb: CallbackQuery, sessions: SessionFactory) -> None:
    chat_id = cb.data[len("cm:tpl:"):]
    text, kb = await _quick_screen(sessions, cb.from_user.id, chat_id)
    await cb.answer()
    await cb.message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("cm:ts:"))
async def quick_send(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    qid, _, chat_id = cb.data[len("cm:ts:"):].partition(":")
    seller = await _connected(sessions, cb.from_user)
    if seller is None:
        await cb.answer("Аккаунт Playerok не подключён.", show_alert=True)
        return
    async with sessions() as session:
        q = await session.get(QuickReply, int(qid)) if qid.isdigit() else None
    if q is None or q.seller_tg_id != seller.tg_id:
        await cb.answer("Шаблон удалён.", show_alert=True)
        return
    s = await _deal_for(sessions, seller.tg_id, "c" + chat_id)
    text = ft.render(q.text, seller, _as_deal(s) if s else None)
    await cb.answer("Отправляю…")
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        ok, _ = await _call(cb, sessions, seller, lambda: client.send_message(chat_id, text))
    if ok:
        try:
            await cb.message.edit_text(f"✅ Отправлено покупателю:\n{ft.quote(text)}")
        except TelegramBadRequest:
            pass


@router.callback_query(F.data.startswith("cm:tadd:"))
async def quick_add(cb: CallbackQuery, state: FSMContext, sessions: SessionFactory) -> None:
    if len(await _quick_list(sessions, cb.from_user.id)) >= MAX_QUICK:
        await cb.answer(f"Не больше {MAX_QUICK} шаблонов — удали лишние.", show_alert=True)
        return
    await state.set_state(QuickAdd.text)
    await state.update_data(chat_id=cb.data[len("cm:tadd:"):])
    await cb.answer()
    await cb.message.answer(
        "Пришли текст шаблона одним сообщением. Начало текста станет названием кнопки.\n\n"
        + ft.VARIABLES_HELP,
        reply_markup=cancel_kb(),
    )


@router.message(QuickAdd.text, F.text)
async def quick_save(message: Message, state: FSMContext, sessions: SessionFactory) -> None:
    text = message.text.strip()
    if not text:
        await message.answer("Пустой текст — пришли ещё раз или нажми «Отмена».")
        return
    chat_id = (await state.get_data()).get("chat_id", "")
    await state.clear()
    first = text.splitlines()[0]
    title = first if len(first) <= 30 else first[:29] + "…"
    async with sessions() as session:
        session.add(QuickReply(seller_tg_id=message.from_user.id, title=title, text=text[:4000]))
        await session.commit()
        seller = await get_or_create_seller(session, message.from_user)
    await message.answer("✅ Шаблон добавлен.", reply_markup=main_menu(seller.is_connected))
    if chat_id:
        screen, kb = await _quick_screen(sessions, message.from_user.id, chat_id)
        await message.answer(screen, reply_markup=kb)


@router.callback_query(F.data.startswith("cm:tdl:"))
async def quick_del_menu(cb: CallbackQuery, sessions: SessionFactory) -> None:
    chat_id = cb.data[len("cm:tdl:"):]
    rows = [[_btn(f"🗑 {q.title}", f"cm:tdx:{q.id}:{chat_id}")] for q in await _quick_list(sessions, cb.from_user.id)]
    rows.append([_btn("‹ Назад", f"cm:tbk:{chat_id}")])
    await cb.answer()
    try:
        await cb.message.edit_text("Какой шаблон удалить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("cm:tdx:"))
async def quick_del(cb: CallbackQuery, sessions: SessionFactory) -> None:
    qid, _, chat_id = cb.data[len("cm:tdx:"):].partition(":")
    async with sessions() as session:
        q = await session.get(QuickReply, int(qid)) if qid.isdigit() else None
        if q is not None and q.seller_tg_id == cb.from_user.id:
            await session.delete(q)
            await session.commit()
    await cb.answer("Удалено")
    await _quick_back(cb, sessions, chat_id)


@router.callback_query(F.data.startswith("cm:tbk:"))
async def quick_back(cb: CallbackQuery, sessions: SessionFactory) -> None:
    await cb.answer()
    await _quick_back(cb, sessions, cb.data[len("cm:tbk:"):])


async def _quick_back(cb: CallbackQuery, sessions: SessionFactory, chat_id: str) -> None:
    text, kb = await _quick_screen(sessions, cb.from_user.id, chat_id)
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ===================== 💬 Весь чат =====================


def _when(value: str, tz: int) -> str:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (dt + timedelta(hours=tz)).strftime("%d.%m %H:%M")


def format_history(messages, own_id: str, tz: int, limit: int = 3800) -> str:
    """Переписка от старых к новым; если длинно — остаются последние сообщения."""
    blocks: list[str] = []
    size = 0
    for m in reversed(messages):
        if m.author_id and m.author_id == own_id:
            who = "🟢 Вы"
        elif m.author_id:
            who = "🔵 " + html.escape(m.author_username or "покупатель")
        else:
            who = "ℹ️ Playerok"
        body = m.text.strip()
        if len(body) > 700:
            body = body[:700] + "…"
        body = html.escape(body)
        if m.images:
            body = (body + "\n" if body else "") + f"🖼 картинка ×{m.images}"
        if not body:
            body = f"<i>{html.escape(m.event or 'событие')}</i>"
        block = f"<b>{who}</b> <i>{_when(m.created_at, tz)}</i>\n{body}"
        if size + len(block) > limit:
            break
        blocks.append(block)
        size += len(block) + 2
    return "\n\n".join(reversed(blocks))


@router.callback_query(F.data.startswith("cm:all:"))
async def chat_history(cb: CallbackQuery, sessions: SessionFactory, cipher: TokenCipher) -> None:
    chat_id = cb.data[len("cm:all:"):]
    seller = await _connected(sessions, cb.from_user)
    if seller is None:
        await cb.answer("Аккаунт Playerok не подключён.", show_alert=True)
        return
    await cb.answer("Загружаю чат…")
    async with PlayerokClient(cipher.decrypt(seller.token_enc)) as client:
        ok, messages = await _call(cb, sessions, seller, lambda: client.chat_messages(chat_id))
    if not ok:
        return
    async with sessions() as session:
        tz = await ft.get_tz(session, seller.tg_id)
    body = format_history(messages or [], seller.playerok_id or "", tz) or "<i>Сообщений нет.</i>"
    url = f"https://playerok.com/chats/{chat_id}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        _btn("✉️ Ответ", f"reply:{chat_id}"), _btn("📋 Шаблоны", f"cm:tpl:{chat_id}"),
    ]])
    await cb.message.answer(
        f"💬 <b>Чат</b> — последние сообщения (<a href=\"{url}\">открыть на сайте</a>)\n\n{body}",
        reply_markup=kb, disable_web_page_preview=True,
    )
