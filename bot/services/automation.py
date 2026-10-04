"""Движок автоматизации: реагирует на изменения сделок и чатов продавца.

Вызывается из поллера на каждом круге. При act=False (первый проход после
входа) только запоминает текущее состояние и ничего не отправляет.
"""

from __future__ import annotations

import html
import re
import unicodedata
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import (
    ActionLog,
    AutoReply,
    ChatState,
    DealState,
    DeliveryItem,
    RelistRule,
    Seller,
    SessionFactory,
)
from ..playerok import AuthRequired, ChatPreview, Deal, Item, PlayerokClient, PlayerokError
from ..crypto import TokenCipher
from ..logs import tag
from . import features as ft
from ..plugins.stars import service as stars_svc
from .notifications import notify

log = logging.getLogger(__name__)

BUYER_CONFIRMED = ("CONFIRMED", "COMPLETED")
PROBLEM_MARKERS = ("PROBLEM", "DISPUTE", "ROLLBACK", "REFUND")
REFUND_MARKERS = ("ROLLBACK", "REFUND")
RELIST_STATUSES = ("SOLD", "EXPIRED")
SOLD_DEAL_STATUSES = ("PAID", "SENT", "CONFIRMED", "COMPLETED")
ITEMS_CHECK_EVERY = timedelta(minutes=10)


def _link(url: str, text: str) -> str:
    return f'<a href="{url}">{text}</a>'


def _now() -> datetime:
    return datetime.utcnow()


def _is_problem(status: str) -> bool:
    return any(m in status.upper() for m in PROBLEM_MARKERS)


def _is_refund(status: str) -> bool:
    return any(m in status.upper() for m in REFUND_MARKERS)


async def _say(client: PlayerokClient, chat_id: str | None, text: str) -> bool:
    if not chat_id or not text.strip():
        return False
    try:
        await client.send_message(chat_id, text)
        return True
    except PlayerokError as e:
        log.warning("Не удалось написать в чат %s: %s", chat_id, e)
        return False


async def _tpl(session: AsyncSession, seller: Seller, kind: str, deal: Deal) -> str:
    text = await ft.get_template(session, seller.tg_id, kind, deal.item_name)
    return ft.render(text, seller, deal)


# ===================== сделки =====================


async def process_deals(
    bot: Bot,
    sessions: SessionFactory,
    seller: Seller,
    client: PlayerokClient,
    deals: list[Deal],
    *,
    act: bool,
) -> None:
    async with sessions() as session:
        for deal in deals:
            if not deal.id:
                continue
            try:
                await _process_deal(bot, session, seller, client, deal, act=act)
            except PlayerokError as e:
                log.warning("%s сделка %s: %s", tag(seller.tg_id), deal.id, e)
            except Exception:
                log.exception("%s сделка %s: сбой обработки", tag(seller.tg_id), deal.id)
            await session.commit()


async def _process_deal(
    bot: Bot,
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    deal: Deal,
    *,
    act: bool,
) -> None:
    tg = seller.tg_id
    state = await session.scalar(
        select(DealState).where(DealState.seller_tg_id == tg, DealState.deal_id == deal.id)
    )
    is_new = state is None
    if state is None:
        state = DealState(
            seller_tg_id=tg,
            deal_id=deal.id,
            chat_id=deal.chat_id,
            item_name=deal.item_name,
            status=deal.status,
            first_seen_at=_now(),
            relisted=False,  # NULL у старых записей = «не трогать», см. перевыставление
        )
        session.add(state)
    if state.chat_id is None and deal.chat_id:
        state.chat_id = deal.chat_id
    _update_analytics(state, deal)

    prev, cur = state.status, deal.status
    if not act:
        # Базовый срез: считаем, что всё текущее уже обработано.
        state.status = cur
        if cur == "SENT" and state.sent_at is None:
            state.sent_at = _now()
        if cur in BUYER_CONFIRMED and state.confirmed_at is None:
            state.confirmed_at = _now()
        if deal.review_rating is not None:
            state.review_thanked = True
            state.review_reminded = True
        state.relisted = True  # старые продажи не выставляем заново
        return

    now = _now()

    # --- новый оплаченный заказ ---
    if cur == "PAID" and (is_new or prev != "PAID"):
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["greeting"]):
            await _say(client, deal.chat_id, await _tpl(session, seller, "greeting", deal))
        if await stars_svc.on_paid(bot, session, seller, client, deal):
            state.delivered = False  # выдача звёзд идёт своим путём, автоподтверждение не трогаем
            state.status = cur
            return

    # --- автовыдача: пробуем, пока заказ оплачен и товар не выдан (например,
    # запас добавили позже или отправка в чат не прошла с первого раза) ---
    if (
        cur == "PAID"
        and not state.delivered
        # не трогаем старые заказы: их продавец мог уже закрыть вручную
        and now - (state.created_at or state.first_seen_at or now) <= timedelta(hours=24)
        and await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["autodelivery"])
    ):
        await _deliver(bot, session, seller, client, deal, state)

    # --- автоподтверждение: по настройкам функции «Автоподтверждение» или сразу
    # после автовыдачи (флаг в разделе «Автовыдача», включён по умолчанию) ---
    if cur == "PAID" and not state.autoconfirm_failed:
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["autoconfirm"]):
            only_delivered = await ft.get_flag(session, tg, "autoconfirm_only_delivered", True)
            delay = await ft.get_param(session, tg, "autoconfirm_delay")
            ready = now - state.first_seen_at >= timedelta(minutes=delay) and (
                state.delivered or not only_delivered
            )
        else:
            ready = bool(state.delivered) and await ft.get_flag(
                session, tg, "autodelivery_confirm", True
            )
        if ready:
            try:
                await client.confirm_deal(deal.id)
            except PlayerokError as e:
                # Сообщаем один раз на заказ, чтобы не спамить каждые 30 секунд.
                state.autoconfirm_failed = True
                log.warning("%s автоподтверждение: Playerok отказал для сделки %s: %s", tag(tg), deal.id, e)
                await notify(
                    bot, session, tg, "problem",
                    f"⚠️ Не смог отметить заказ «{html.escape(deal.item_name)}» выполненным: "
                    f"<code>{html.escape(str(e))[:300]}</code>\n"
                    f"Отметь вручную: {_link(deal.deal_url, 'Открыть заказ')}\n"
                    "Пришли этот текст разработчику — поправлю запрос.",
                )
            else:
                state.sent_at = now
                cur = "SENT"
                log.info("%s автоподтверждение: сделка %s отмечена выполненной", tag(tg), deal.id)
                if await ft.get_flag(session, tg, "autoconfirm_msg_enabled", True):
                    await _say(client, deal.chat_id, await _tpl(session, seller, "autoconfirm_msg", deal))
                await notify(
                    bot, session, tg, "system",
                    f"🤝 Автоподтверждение: заказ «{html.escape(deal.item_name)}» отмечен выполненным.",
                )
    # --- продавец подтвердил вручную ---
    elif cur == "SENT" and prev != "SENT":
        state.sent_at = state.sent_at or now
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["after_seller_confirm"]):
            await _say(client, deal.chat_id, await _tpl(session, seller, "after_seller_confirm", deal))

    # --- лот продан → выставить заново (по сделке, без списка лотов) ---
    if cur in SOLD_DEAL_STATUSES and state.relisted is not True:
        reason = await _relist_skip_reason(session, tg, deal, state, now)
        if reason is None:
            log.info("%s перевыставление: сделка %s, лот %s «%s» — выставляю",
                     tag(tg), deal.id, deal.item_id, deal.item_name)
            await relist_after_sale(bot, session, seller, client, deal, state)
        else:
            _log_once(tg, deal.id, reason,
                      "%s перевыставление: сделка %s «%s» пропущена — %s",
                      tag(tg), deal.id, deal.item_name, reason)

    # --- покупатель подтвердил ---
    if cur in BUYER_CONFIRMED and prev not in BUYER_CONFIRMED:
        state.confirmed_at = state.confirmed_at or now
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["after_buyer_confirm"]):
            await _say(client, deal.chat_id, await _tpl(session, seller, "after_buyer_confirm", deal))
        review = ""
        if deal.review_rating is not None:
            stars = "⭐" * max(1, min(5, deal.review_rating))
            txt = f"\n<b>Текст отзыва:</b> {html.escape(deal.review_text)}" if deal.review_text else ""
            review = f"\n<b>Отзыв от клиента — есть</b>{txt}\n<b>Оценка:</b> {deal.review_rating} {stars}"
        else:
            review = "\n<b>Отзыв от клиента</b> — пока нет"
        await notify(
            bot, session, tg, "confirmed",
            f"✅ <b>Подтверждение сделки для {html.escape(seller.playerok_username or '—')}</b>\n\n"
            f"<b>Товар:</b> {_link(deal.item_url, html.escape(deal.item_name))}\n\n"
            f"<b>Заказ:</b> {_link(deal.deal_url, 'Открыть заказ')}\n"
            f"<b>Покупатель:</b> {html.escape(deal.buyer_username)}\n"
            f"{review}",
        )

    # --- проблема / спор ---
    if _is_problem(cur) and not _is_problem(prev):
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["problem"]):
            await _say(client, deal.chat_id, await _tpl(session, seller, "problem", deal))
        refund = _is_refund(cur)
        title = "💸 Возврат средств" if refund else "⚠️ Проблема"
        await notify(
            bot, session, tg, "refund" if refund else "problem",
            f"{title} по заказу «{html.escape(deal.item_name)}» "
            f"(покупатель {html.escape(deal.buyer_username)}), статус {html.escape(cur)}.\n{deal.deal_url}",
        )

    # --- отзыв ---
    if deal.review_rating is not None and not state.review_thanked:
        state.review_thanked = True
        state.review_reminded = True
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["after_review"]):
            kind = "review_good" if deal.review_rating >= 4 else "review_bad"
            await _say(client, deal.chat_id, await _tpl(session, seller, kind, deal))
        stars = "⭐" * max(1, min(5, deal.review_rating))
        body = f"\n<b>Текст отзыва:</b> {html.escape(deal.review_text)}" if deal.review_text else "\n<b>Текст отзыва:</b>"
        await notify(
            bot, session, tg, "review",
            f"⭐ <b>Новый отзыв для {html.escape(seller.playerok_username or '—')}</b>\n\n"
            f"<b>Ссылка на заказ:</b> {_link(deal.deal_url, 'Открыть заказ')}\n"
            f"<b>Аккаунт покупателя:</b> {html.escape(deal.buyer_username)}\n"
            f"{body}\n<b>Оценка:</b> {deal.review_rating} {stars}",
        )

    # --- напоминания ---
    if cur == "SENT" and state.sent_at:
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["confirm_reminder"]):
            if state.confirm_reminded_at is None:
                first = await ft.get_param(session, tg, "confirm_reminder_minutes")
                due = now - state.sent_at >= timedelta(minutes=first)
            elif await ft.get_flag(session, tg, "confirm_reminder_cyclic", True):
                repeat = await ft.get_param(session, tg, "confirm_reminder_repeat_minutes")
                due = now - state.confirm_reminded_at >= timedelta(minutes=repeat)
            else:
                due = False
            if due:
                state.confirm_reminded_at = now
                state.confirm_reminders = (state.confirm_reminders or 0) + 1
                await _say(client, deal.chat_id, await _tpl(session, seller, "confirm_reminder", deal))
    if (
        cur in BUYER_CONFIRMED
        and state.confirmed_at
        and not state.review_reminded
        and deal.review_rating is None
    ):
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["review_reminder"]):
            hours = await ft.get_param(session, tg, "review_reminder_hours")
            if now - state.confirmed_at >= timedelta(hours=hours):
                state.review_reminded = True
                await _say(client, deal.chat_id, await _tpl(session, seller, "review_reminder", deal))

    state.status = cur


def _parse_dt(value: str) -> datetime | None:
    """ISO-дата Playerok → naive UTC (как остальные даты в базе)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _update_analytics(state: DealState, deal: Deal) -> None:
    if isinstance(deal.price, (int, float)):
        state.price = float(deal.price)
    if deal.buyer_username:
        state.buyer = deal.buyer_username[:64]
    if state.created_at is None:
        state.created_at = _parse_dt(deal.created_at) or state.first_seen_at or _now()
    if deal.review_rating is not None:
        state.review_rating = deal.review_rating
    if deal.item_name:
        state.item_name = deal.item_name


def norm_lot(name: str | None) -> str:
    """Название лота без эмодзи, знаков, регистра и лишних пробелов —
    чтобы «💰 50 РОБУКСОВ | ПРОМОКОД 💎» совпало с «50 робуксов промокод»."""
    s = unicodedata.normalize("NFKC", name or "").lower().replace("ё", "е")
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


async def match_stock_key(
    session: AsyncSession, tg: int, item_name: str, *, only_free: bool
) -> str | None:
    """item_key запаса, который относится к лоту сделки. Сначала точное совпадение
    нормализованных названий, затем вхождение одного в другое (самое длинное)."""
    q = select(DeliveryItem.item_key, DeliveryItem.item_name).where(DeliveryItem.seller_tg_id == tg)
    if only_free:
        q = q.where(DeliveryItem.used_deal_id.is_(None))
    rows = (await session.execute(q.distinct())).all()
    target = norm_lot(item_name)
    if not target:
        return None
    best: tuple[int, str] | None = None
    for key, name in rows:
        n = norm_lot(name)
        if not n:
            continue
        if n == target:
            return key
        short = min(len(n), len(target))
        # по целым словам: «50 робуксов» не должно совпасть с «150 робуксов»
        if short >= 6 and (f" {n} " in f" {target} " or f" {target} " in f" {n} "):
            if best is None or len(n) > best[0]:
                best = (len(n), key)
    return best[1] if best else None


async def _deliver(
    bot: Bot,
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    deal: Deal,
    state: DealState,
) -> None:
    if state.delivered:
        return
    key = await match_stock_key(session, seller.tg_id, deal.item_name, only_free=True)
    item = None
    if key is not None:
        item = await session.scalar(
            select(DeliveryItem)
            .where(
                DeliveryItem.seller_tg_id == seller.tg_id,
                DeliveryItem.used_deal_id.is_(None),
                DeliveryItem.item_key == key,
            )
            .order_by(DeliveryItem.id)
            .limit(1)
        )
    if item is None:
        # Запас для лота когда-то был — значит кончился: покупатель ждёт.
        # Запаса не было вовсе — лот без автовыдачи, молчим.
        if await match_stock_key(session, seller.tg_id, deal.item_name, only_free=False) and not state.stock_alerted:
            state.stock_alerted = True
            await notify(
                bot, session, seller.tg_id, "out_of_stock",
                f"📭 <b>Заказ без товара!</b> Запас «{html.escape(deal.item_name)}» пуст, "
                f"покупатель {html.escape(deal.buyer_username)} ждёт.\n"
                f"Выдай вручную или пополни запас: {_link(deal.deal_url, 'Открыть заказ')}",
            )
        return
    if not deal.chat_id:
        if not state.stock_alerted:
            state.stock_alerted = True
            await notify(
                bot, session, seller.tg_id, "problem",
                f"⚠️ Не нашёл чат заказа «{html.escape(deal.item_name)}», товар не выдан. "
                f"Выдай вручную: {_link(deal.deal_url, 'Открыть заказ')}",
            )
        return
    intro = await _tpl(session, seller, "delivery_msg", deal)
    text = f"{intro}\n{item.content}" if intro.strip() else item.content
    if not await _say(client, deal.chat_id, text):
        return
    item.used_deal_id = deal.id
    item.used_at = _now()
    state.delivered = True
    log.info("%s автовыдача: сделка %s «%s» — товар выдан", tag(seller.tg_id), deal.id, deal.item_name)
    left = await session.scalar(
        select(func.count(DeliveryItem.id)).where(
            DeliveryItem.seller_tg_id == seller.tg_id,
            DeliveryItem.used_deal_id.is_(None),
            DeliveryItem.item_key == item.item_key,
        )
    )
    await notify(
        bot, session, seller.tg_id, "system",
        f"📦 Автовыдача: «{html.escape(deal.item_name)}» выдан "
        f"{html.escape(deal.buyer_username)}. Осталось: {left}.",
    )
    if not left:
        await notify(
            bot, session, seller.tg_id, "out_of_stock",
            f"📭 Товар для «{html.escape(deal.item_name)}» закончился. "
            "Пополни запас в настройках автовыдачи.",
        )


# ===================== чаты =====================


async def process_chats(
    sessions: SessionFactory,
    seller: Seller,
    client: PlayerokClient,
    chats: list[ChatPreview],
    *,
    act: bool,
    bot: Bot | None = None,
    cipher: TokenCipher | None = None,
) -> None:
    async with sessions() as session:
        for chat in chats:
            try:
                await _process_chat(session, seller, client, chat, act=act, bot=bot, cipher=cipher)
            except PlayerokError as e:
                log.warning("%s чат %s: %s", tag(seller.tg_id), chat.id, e)
            except Exception:
                log.exception("%s чат %s: сбой обработки", tag(seller.tg_id), chat.id)
            await session.commit()


async def _process_chat(
    session: AsyncSession,
    seller: Seller,
    client: PlayerokClient,
    chat: ChatPreview,
    *,
    act: bool,
    bot: Bot | None = None,
    cipher: TokenCipher | None = None,
) -> None:
    tg = seller.tg_id
    from_buyer = chat.last_message_id and chat.last_author_id != seller.playerok_id
    if not from_buyer or chat.unread <= 0:
        return  # либо последним писал продавец, либо всё прочитано
    state = await session.scalar(
        select(ChatState).where(ChatState.seller_tg_id == tg, ChatState.chat_id == chat.id)
    )
    if state is None:
        state = ChatState(seller_tg_id=tg, chat_id=chat.id)
        session.add(state)
    now = _now()
    is_new_message = state.last_buyer_message_id != chat.last_message_id
    if is_new_message:
        state.last_buyer_message_id = chat.last_message_id
        state.last_buyer_message_at = now
    if not act:
        state.ignore_sent_for = chat.last_message_id
        return

    if is_new_message:
        # Ждём @username для выдачи звёзд?
        if bot is not None and cipher is not None and await stars_svc.on_buyer_message(
            bot, session, seller, client, cipher, chat.id, chat.last_text
        ):
            state.ignore_sent_for = chat.last_message_id
            return
        # Автоответчик по ключевым словам
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["autoresponder"]):
            text = chat.last_text.lower()
            rules = await session.scalars(select(AutoReply).where(AutoReply.seller_tg_id == tg))
            for rule in rules:
                if rule.keyword.lower() in text:
                    reply = ft.render(rule.text, seller, buyer=chat.last_author_username)
                    if await _say(client, chat.id, reply):
                        state.ignore_sent_for = chat.last_message_id
                        return
        # Текст «не в сети»
        if await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["offline"]) and await ft.get_flag(
            session, tg, "offline_mode"
        ):
            repeat = await ft.get_param(session, tg, "offline_repeat_hours")
            if state.offline_sent_at is None or now - state.offline_sent_at >= timedelta(hours=repeat):
                text = await ft.get_template(session, tg, "offline")
                if await _say(client, chat.id, ft.render(text, seller, buyer=chat.last_author_username)):
                    state.offline_sent_at = now

    # Текст при игноре: сообщение всё ещё без ответа дольше N минут
    if (
        state.last_buyer_message_at
        and state.ignore_sent_for != chat.last_message_id
        and await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["ignore"])
    ):
        minutes = await ft.get_param(session, tg, "ignore_minutes")
        if now - state.last_buyer_message_at >= timedelta(minutes=minutes):
            text = await ft.get_template(session, tg, "ignore")
            if await _say(client, chat.id, ft.render(text, seller, buyer=chat.last_author_username)):
                state.ignore_sent_for = chat.last_message_id


# ===================== лоты: поднятие и перевыставление =====================


async def process_items(
    bot: Bot, sessions: SessionFactory, seller: Seller, client: PlayerokClient
) -> None:
    tg = seller.tg_id
    async with sessions() as session:
        bump_on = await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["bump"])
        relist_on = await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["relist"])
        if not (bump_on or relist_on):
            return
        if await _last_action(session, tg, "items_check") and _now() - (
            await _last_action(session, tg, "items_check")
        ) < ITEMS_CHECK_EVERY:
            return
        session.add(ActionLog(seller_tg_id=tg, kind="items_check"))
        await session.commit()

        # Ошибки запроса лотов НЕ должны разлогинивать продавца: продажи и чаты
        # работают отдельно, а поднятие/перевыставление — вторично.
        try:
            items = await client.my_items(seller.playerok_id or "")
        except (AuthRequired, PlayerokError) as e:
            # Пишем не чаще раза в 6 ч: перевыставление после продажи этот список
            # не использует, от него зависят только автоподнятие и ручной просмотр.
            now_ = _now()
            last = _ITEMS_WARNED.get(tg)
            if last is None or now_ - last > timedelta(hours=6):
                _ITEMS_WARNED[tg] = now_
                log.warning(
                    "%s список лотов недоступен (%s) — автоподнятие и «Показать лоты» не работают; "
                    "перевыставление после продажи работает без списка",
                    tag(tg), e,
                )
            return
        now = _now()

        if bump_on:
            interval = timedelta(hours=await ft.get_param(session, tg, "bump_interval_hours"))
            limit = await ft.get_param(session, tg, "bump_daily_limit")
            cost = await ft.get_param(session, tg, "bump_cost")
            spent = await _spent_today(session, tg, "bump")
            for item in items:
                if item.status and item.status.upper() != "APPROVED" and item.status.upper() != "ACTIVE":
                    continue
                last = await _last_action(session, tg, "bump", item.id)
                if last and now - last < interval:
                    continue
                if spent + cost > limit:
                    break
                try:
                    await client.bump_item(item.id)
                except PlayerokError as e:
                    log.warning("%s поднятие лота %s: %s", tag(tg), item.id, e)
                    continue
                spent += cost
                session.add(ActionLog(seller_tg_id=tg, kind="bump", target=item.id, cost=cost))
                await session.commit()
                await notify(bot, session, tg, "system", f"🚀 Поднял лот «{html.escape(item.name)}».")

        if relist_on:
            interval = timedelta(hours=await ft.get_param(session, tg, "relist_interval_hours"))
            candidates = await relist_candidates(session, tg, items)
            for item in candidates:
                last = await _last_action(session, tg, "relist", item.id)
                if last and now - last < interval:
                    continue
                try:
                    await client.publish_item(item.id)
                except PlayerokError as e:
                    log.warning("%s перевыставление (по списку) лота %s: %s", tag(tg), item.id, e)
                    continue
                session.add(ActionLog(seller_tg_id=tg, kind="relist", target=item.id))
                await session.commit()
                await notify(
                    bot, session, tg, "relisted", f"🔁 Выставил заново лот «{html.escape(item.name)}»."
                )


_LOGGED: set[tuple[int, str, str]] = set()
_ITEMS_WARNED: dict[int, datetime] = {}


def _log_once(tg: int, deal_id: str, reason: str, msg: str, *args) -> None:
    """Пишет причину пропуска один раз на сделку, а не каждые 30 секунд."""
    key = (tg, deal_id, reason)
    if key in _LOGGED:
        return
    if len(_LOGGED) > 20000:
        _LOGGED.clear()
    _LOGGED.add(key)
    log.info(msg, *args)


async def _relist_skip_reason(
    session: AsyncSession, tg: int, deal: Deal, state: DealState, now: datetime
) -> str | None:
    """None — выставлять; иначе человекочитаемая причина, почему нет."""
    if state.relisted is None:
        return "сделка была в базе до включения перевыставления по сделке"
    if not deal.item_id:
        return "Playerok не вернул ID лота в сделке"
    if now - (state.created_at or state.first_seen_at or now) > timedelta(hours=48):
        return "сделка старше 48 ч"
    if not await ft.is_enabled(session, tg, ft.FEATURE_BY_KEY["relist"]):
        return "функция «Автовыставление лотов» выключена"
    if not await relist_allowed(session, tg, deal.item_name, deal.item_url):
        return "лот не подходит под правила отбора"
    return None


async def relist_allowed(session: AsyncSession, tg: int, name: str, url: str) -> bool:
    """Подходит ли лот под режим «все лоты» или под правила отбора."""
    if await ft.get_flag(session, tg, "relist_all", True):
        return True
    hay = f"{name} {url}".lower()
    patterns = await session.scalars(select(RelistRule.pattern).where(RelistRule.seller_tg_id == tg))
    return any(p.lower() in hay for p in patterns)


async def relist_after_sale(
    bot: Bot, session: AsyncSession, seller: Seller, client: PlayerokClient, deal: Deal, state: DealState
) -> None:
    """Выставляет проданный лот заново прямо по сделке — без запроса списка лотов,
    который Playerok может не отдавать. Один раз на сделку."""
    tg = seller.tg_id
    state.relisted = True
    try:
        await client.publish_item(deal.item_id)
    except PlayerokError as e:
        log.warning("%s перевыставление: Playerok отказал для лота %s (сделка %s): %s",
                    tag(tg), deal.item_id, deal.id, e)
        await notify(
            bot, session, tg, "problem",
            f"⚠️ Не смог выставить заново «{html.escape(deal.item_name)}»: "
            f"<code>{html.escape(str(e))[:300]}</code>\n"
            f"Выставь вручную: {_link(deal.item_url, 'Открыть лот')}\n"
            "Пришли этот текст разработчику — поправлю запрос.",
        )
        return
    session.add(ActionLog(seller_tg_id=tg, kind="relist", target=deal.item_id))
    log.info("%s перевыставление: лот %s выставлен заново (сделка %s)", tag(tg), deal.item_id, deal.id)
    await notify(
        bot, session, tg, "relisted",
        f"🔁 Выставил заново «{html.escape(deal.item_name)}» — {_link(deal.item_url, 'Открыть лот')}",
    )


async def relist_candidates(session: AsyncSession, tg: int, items: list[Item]) -> list[Item]:
    """Проданные лоты, которые подходят под режим и правила автовыставления."""
    all_lots = await ft.get_flag(session, tg, "relist_all", True)
    paid_ok = await ft.get_flag(session, tg, "relist_paid_allowed", False)
    patterns = [
        r.pattern.lower()
        for r in await session.scalars(select(RelistRule).where(RelistRule.seller_tg_id == tg))
    ]
    result = []
    for item in items:
        if item.status.upper() not in RELIST_STATUSES:
            continue
        if item.is_paid_relist and not paid_ok:
            continue
        if not all_lots:
            hay = f"{item.name} {item.url}".lower()
            if not any(p in hay for p in patterns):
                continue
        result.append(item)
    return result


async def _last_action(
    session: AsyncSession, tg: int, kind: str, target: str = ""
) -> datetime | None:
    return await session.scalar(
        select(func.max(ActionLog.created_at)).where(
            ActionLog.seller_tg_id == tg, ActionLog.kind == kind, ActionLog.target == target
        )
    )


async def _spent_today(session: AsyncSession, tg: int, kind: str) -> int:
    since = _now() - timedelta(hours=24)
    total = await session.scalar(
        select(func.coalesce(func.sum(ActionLog.cost), 0)).where(
            ActionLog.seller_tg_id == tg, ActionLog.kind == kind, ActionLog.created_at >= since
        )
    )
    return int(total or 0)
