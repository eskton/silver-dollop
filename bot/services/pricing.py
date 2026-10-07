"""Снижение цен: держит выбранные лоты дешевле конкурентов.

Как работает (раз в `dumping_interval_min` минут):
  1) свой активный лот находится по правилу (ссылка/slug или название);
  2) сохранённый запрос `item` даёт категорию, цену для покупателя и rawPrice;
  3) берутся лоты всех продавцов этой категории (`items` с фильтром gameCategoryId),
     отбираются похожие по ключевым словам правила, свои — исключаются;
  4) цель = минимальная цена конкурента − шаг, но не ниже минимума продавца;
  5) если своя цена выше цели — updateItem с новой rawPrice. Цену только снижаем.
Цены в правиле — «для покупателя», как на сайте; rawPrice пересчитывается по
текущему соотношению price/rawPrice лота (комиссия площадки).
"""

from __future__ import annotations

import html
import logging
import math
from datetime import datetime, timedelta

from aiogram import Bot
from sqlalchemy import select

from ..db import PriceRule, Seller, SessionFactory
from ..logs import tag
from ..playerok import AuthRequired, Item, PlayerokClient, PlayerokError
from . import features as ft
from .notifications import notify

log = logging.getLogger(__name__)

LAST_RUN_KEY = "dumping_last_run"


def _norm(text: str | None) -> str:
    from .automation import norm_lot  # ленивый импорт: automation импортирует много

    return norm_lot(text)


def kw_match(keyword: str, name: str) -> bool:
    """Все слова ключа есть в названии. Числа — целым словом (100 ≠ 1000),
    остальное — частью слова («робукс» найдёт «робуксов»)."""
    words = _norm(keyword).split()
    n = _norm(name)
    tokens = n.split()
    for w in words:
        if w.isdigit():
            if w not in tokens:
                return False
        elif w not in n:
            return False
    return bool(words)


def find_my_lot(items: list[Item], rule: PriceRule) -> Item | None:
    key = rule.lot_key.strip()
    for it in items:
        if key in (it.id, it.slug) or (it.slug and key.rstrip("/").endswith("/" + it.slug)):
            return it
    target = _norm(key)
    exact = [it for it in items if _norm(it.name) == target]
    if exact:
        return exact[0]
    loose = [it for it in items if kw_match(key, it.name)]
    return loose[0] if len(loose) == 1 else None


def _rub(v: float) -> str:
    return f"{v:,.2f}".rstrip("0").rstrip(".").replace(",", " ") + " ₽"


def _who(o: Item) -> str:
    """Какой лот конкурента взят для сравнения — чтобы продавец мог проверить."""
    return f"«{o.name[:40]}» {o.url}"


async def check_rule(
    client: PlayerokClient, seller: Seller, rule: PriceRule, my_items: list[Item]
) -> tuple[str, bool]:
    """(пометка для экрана, изменили ли цену)."""
    mine = find_my_lot(my_items, rule)
    if mine is None:
        return "мой лот не найден среди активных — проверь название", False
    item = await client.get_item(mine.id)
    price, raw = item.price, item.raw_price
    if not isinstance(price, (int, float)) or not isinstance(raw, (int, float)) or price <= 0 or raw <= 0:
        return "Playerok не отдал цену лота", False
    if not item.category_id:
        return "Playerok не отдал категорию лота", False
    others = await client.category_items(item.category_id)
    rivals = [
        o for o in others
        if o.id != item.id
        and o.user_id != (seller.playerok_id or "")
        and isinstance(o.price, (int, float)) and o.price > 0
        and kw_match(rule.competitor_kw, o.name)
    ]
    if not rivals:
        return f"конкурентов «{rule.competitor_kw}» не найдено, цена {_rub(price)}", False
    best = min(rivals, key=lambda o: o.price)
    at_min = best.price - rule.step < rule.min_price
    target = max(best.price - rule.step, rule.min_price)
    if price <= target:
        why = f"на минимуме {_rub(rule.min_price)}" if at_min else "дешевле всех"
        return f"{why}: моя {_rub(price)}, у конкурента {_rub(best.price)} — {_who(best)}", False
    k = price / raw  # множитель комиссии площадки
    new_raw = math.floor(target / k)
    new_raw = max(new_raw, math.ceil(rule.min_price / k) if rule.min_price else 1)
    if new_raw >= raw:
        why = f"на минимуме {_rub(rule.min_price)}" if at_min else "снижать некуда"
        return f"{why}: моя {_rub(price)}, у конкурента {_rub(best.price)} — {_who(best)}", False
    answer = await client.update_item_price(item.id, new_raw)
    # Проверяем, что Playerok действительно поменял цену, а не просто ответил «ок».
    after = await client.get_item(item.id)
    status = str(answer.get("status") or after.status or "")
    if not isinstance(after.price, (int, float)) or after.price >= price:
        return (
            f"Playerok принял запрос, но цена не изменилась: {_rub(price)} "
            f"(статус {status or '?'}, ответ rawPrice={answer.get('rawPrice')}, price={answer.get('price')}). "
            f"Конкурент {_rub(best.price)} — {_who(best)}"
        ), False
    note = f"снизил {_rub(price)} → {_rub(after.price)} (конкурент {_rub(best.price)} — {_who(best)})"
    if status and status.upper() not in ("APPROVED", "ACTIVE"):
        note += f"; статус лота: {status}"
    return note, True


async def run(
    bot: Bot | None, sessions: SessionFactory, seller: Seller, client: PlayerokClient, *, force: bool = False
) -> list[tuple[PriceRule, str, bool]]:
    """Проверяет все правила продавца. Без force — не чаще интервала из настроек."""
    tg = seller.tg_id
    async with sessions() as session:
        feature = ft.FEATURE_BY_KEY["dumping"]
        if not force and not await ft.is_enabled(session, tg, feature):
            return []
        rules = list(await session.scalars(select(PriceRule).where(PriceRule.seller_tg_id == tg)))
        if not rules:
            return []
        now = datetime.utcnow()
        if not force:
            interval = timedelta(minutes=await ft.get_param(session, tg, "dumping_interval_min"))
            raw_last = await ft.get_setting(session, tg, LAST_RUN_KEY, "")
            try:
                last = datetime.fromisoformat(raw_last) if raw_last else None
            except ValueError:
                last = None
            if last and now - last < interval:
                return []
        await ft.set_setting(session, tg, LAST_RUN_KEY, now.isoformat())

        my_items = await client.my_items(seller.playerok_id or "", limit=96, statuses=["APPROVED"])
        results = []
        for rule in rules:
            try:
                note, changed = await check_rule(client, seller, rule, my_items)
            except AuthRequired:
                raise
            except PlayerokError as e:
                note, changed = f"ошибка: {e}"[:500], False
            rule.last_note = note[:600]
            rule.last_run = now
            results.append((rule, note, changed))
            log.info("%s снижение цен «%s»: %s", tag(tg), rule.lot_key[:60], note)
            if changed and bot is not None:
                await notify(bot, session, tg, "system",
                             f"📉 «{html.escape(rule.lot_key[:60])}»: {html.escape(note)}")
        await session.commit()
        return results
