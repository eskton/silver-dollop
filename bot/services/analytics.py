"""Аналитика продавца по данным, которые бот накопил в базе.

Считается только то, что бот видел: заказы с момента подключения аккаунта
(плюс последние ~100 заказов, которые он подтягивает при входе).
"""

from __future__ import annotations

import html
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import ActionLog, DealState, DeliveryItem

# Заказ считается продажей, если он оплачен и не возвращён.
SALE_STATUSES = {"PAID", "SENT", "CONFIRMED", "COMPLETED"}
PROBLEM_MARKERS = ("PROBLEM", "DISPUTE", "ROLLBACK", "REFUND")
LOW_STOCK = 3


@dataclass
class Period:
    title: str
    orders: int = 0
    revenue: float = 0.0
    buyers: set[str] = field(default_factory=set)

    @property
    def avg(self) -> float:
        return self.revenue / self.orders if self.orders else 0.0


def _money(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") + " ₽"


def _is_problem(status: str) -> bool:
    return any(m in status.upper() for m in PROBLEM_MARKERS)


async def build_report(session: AsyncSession, tg_id: int, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    deals = list(await session.scalars(select(DealState).where(DealState.seller_tg_id == tg_id)))

    periods = {
        "today": Period("Сегодня"),
        "week": Period("7 дней"),
        "month": Period("30 дней"),
        "all": Period("Всё время"),
    }
    bounds = {
        "today": now.replace(hour=0, minute=0, second=0, microsecond=0),
        "week": now - timedelta(days=7),
        "month": now - timedelta(days=30),
        "all": datetime.min,
    }
    statuses: Counter[str] = Counter()
    top: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    ratings: list[int] = []
    delivered = 0
    first_order: datetime | None = None

    for d in deals:
        status = (d.status or "").upper()
        when = d.created_at or d.first_seen_at
        if d.review_rating is not None:
            ratings.append(d.review_rating)
        if _is_problem(status):
            statuses["problem"] += 1
            continue
        if status not in SALE_STATUSES:
            continue
        statuses[status] += 1
        if d.delivered and when and when >= bounds["month"]:
            delivered += 1
        if when and (first_order is None or when < first_order):
            first_order = when
        price = d.price or 0.0
        for key, period in periods.items():
            if when and when >= bounds[key]:
                period.orders += 1
                period.revenue += price
                if d.buyer:
                    period.buyers.add(d.buyer)
        if when and when >= bounds["month"]:
            top[d.item_name or "без названия"][0] += 1
            top[d.item_name or "без названия"][1] += price

    if not deals:
        return (
            "📊 <b>Аналитика</b>\n\n"
            "Пока нет данных. Бот собирает статистику по заказам с момента "
            "подключения аккаунта — загляни сюда после первых продаж."
        )

    lines = ["📊 <b>Аналитика продаж</b>", ""]
    for p in periods.values():
        if p.orders:
            lines.append(
                f"<b>{p.title}:</b> {p.orders} зак. · {_money(p.revenue)} · "
                f"ср. чек {_money(p.avg)} · покупателей {len(p.buyers)}"
            )
        else:
            lines.append(f"<b>{p.title}:</b> продаж нет")

    lines += [
        "",
        "<b>Заказы сейчас:</b>",
        f"• ждут выдачи: {statuses['PAID']}",
        f"• ждут подтверждения покупателем: {statuses['SENT']}",
        f"• завершено: {statuses['CONFIRMED'] + statuses['COMPLETED']}",
        f"• проблемы и возвраты: {statuses['problem']}",
    ]

    if top:
        lines += ["", "🏆 <b>Топ лотов за 30 дней:</b>"]
        best = sorted(top.items(), key=lambda kv: (kv[1][1], kv[1][0]), reverse=True)[:5]
        for i, (name, (count, revenue)) in enumerate(best, 1):
            lines.append(f"{i}. {html.escape(name[:40])} — {int(count)} шт. · {_money(revenue)}")

    if ratings:
        good = sum(1 for r in ratings if r >= 4)
        lines += [
            "",
            f"⭐ <b>Отзывы:</b> {len(ratings)} шт., средняя оценка "
            f"{sum(ratings) / len(ratings):.1f}, хороших {good * 100 // len(ratings)}%",
        ]

    month_ago = now - timedelta(days=30)
    bumps, bump_cost = (
        await session.execute(
            select(func.count(ActionLog.id), func.coalesce(func.sum(ActionLog.cost), 0)).where(
                ActionLog.seller_tg_id == tg_id,
                ActionLog.kind == "bump",
                ActionLog.created_at >= month_ago,
            )
        )
    ).one()
    relists = await session.scalar(
        select(func.count(ActionLog.id)).where(
            ActionLog.seller_tg_id == tg_id,
            ActionLog.kind == "relist",
            ActionLog.created_at >= month_ago,
        )
    )
    lines += [
        "",
        "🤖 <b>Автоматизация за 30 дней:</b>",
        f"• выдано автовыдачей: {delivered}",
        f"• поднятий лотов: {bumps} (потрачено {_money(float(bump_cost))})",
        f"• перевыставлений: {relists or 0}",
    ]

    stock = (
        await session.execute(
            select(DeliveryItem.item_name, func.sum(DeliveryItem.used_deal_id.is_(None)))
            .where(DeliveryItem.seller_tg_id == tg_id)
            .group_by(DeliveryItem.item_key)
        )
    ).all()
    low = [(name, int(n or 0)) for name, n in stock if int(n or 0) <= LOW_STOCK]
    if low:
        lines += ["", "⚠️ <b>Заканчивается запас автовыдачи:</b>"]
        lines += [f"• {html.escape(name[:40])} — осталось {n}" for name, n in low]

    if first_order:
        lines += ["", f"<i>Данные с {first_order:%d.%m.%Y}, время UTC.</i>"]
    return "\n".join(lines)
