"""Разделы закупки для «Выгоды по рынку»: бренд → страна → номиналы и закупка в $.

Один раздел активен (настройка `market_section`): по нему считают отчёт и трекер.
Без разделов работает старая схема — общий список «номинал цена» и общий лот.
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import CostEntry, CostSection
from . import features as ft

ACTIVE_KEY = "market_section"

BRANDS: dict[str, str] = {
    "apple": "🍏 Apple",
    "psn": "🎮 PlayStation",
    "xbox": "🟩 Xbox",
    "nintendo": "🍄 Nintendo",
    "steam": "💨 Steam",
    "roblox": "🧱 Roblox",
}

# ключ → (название, слова страны в названиях лотов конкурентов, валюты номинала)
COUNTRIES: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "us": ("🇺🇸 США", "usa, сша, us, america, америк", ("USD",)),
    "tr": ("🇹🇷 Турция", "turkey, turkiye, турц, tr, tl", ("TRY",)),
    "eu": ("🇪🇺 Европа", "europe, европ, eu, euro, евро", ("EUR",)),
    "de": ("🇩🇪 Германия", "germany, герман, de", ("EUR",)),
    "gb": ("🇬🇧 Англия", "uk, britain, англ, великобрит, gbp", ("GBP",)),
    "pl": ("🇵🇱 Польша", "poland, польш, pln", ("PLN",)),
    "in": ("🇮🇳 Индия", "india, инди, inr", ("INR",)),
    "ae": ("🇦🇪 ОАЭ", "uae, оаэ, эмират, aed", ("AED",)),
    "sa": ("🇸🇦 Саудовская Аравия", "saudi, ksa, саудов, sar", ("SAR",)),
    "br": ("🇧🇷 Бразилия", "brazil, brasil, бразил, brl", ("BRL",)),
    "jp": ("🇯🇵 Япония", "japan, япон, jpy", ("JPY",)),
    "kz": ("🇰🇿 Казахстан", "kazakhstan, казах, kz, kzt", ("KZT",)),
    "ar": ("🇦🇷 Аргентина", "argentina, аргент, ars", ("ARS",)),
    "ua": ("🇺🇦 Украина", "ukraine, украин, ua, uah", ("UAH",)),
    "ru": ("🇷🇺 Россия", "russia, росси, рф, ru", ("RUB",)),
    "any": ("🌐 Без страны", "", ()),
}

# слова бренда в названиях лотов — чтобы найти свой лот для сравнения без ссылки
BRAND_WORDS: dict[str, tuple[str, ...]] = {
    "apple": ("apple", "itunes", "app store", "эпл", "эппл"),
    "psn": ("playstation", "psn", "ps store", "плейстейшн"),
    "xbox": ("xbox", "иксбокс"),
    "nintendo": ("nintendo", "eshop", "нинтендо"),
    "steam": ("steam", "стим"),
    "roblox": ("robux", "roblox", "робукс", "роблокс"),
}


def _words(text: str) -> list[str]:
    return [w.strip() for w in text.split(",") if w.strip()]


def country_title(key: str) -> str:
    return COUNTRIES[key][0] if key in COUNTRIES else f"📍 {key}"


def section_title(sec: CostSection) -> str:
    return f"{BRANDS.get(sec.brand, sec.brand)} · {country_title(sec.country)}"


def kw_list(sec: CostSection | None) -> list[str]:
    return [w.strip() for w in (sec.kw if sec else "").split(",") if w.strip()]


def country_match(name: str, words: list[str]) -> bool:
    """В названии лота есть слово страны. Короткие (≤3 букв: us, tr, tl) — только целым
    словом, длинные — частью слова («турц» найдёт «Турция»)."""
    if not words:
        return True
    from .automation import norm_lot  # ленивый импорт: automation тянет много

    n = norm_lot(name)
    tokens = set(n.split())
    for w in words:
        w = norm_lot(w)
        if not w:
            continue
        if (w in tokens) if len(w) <= 3 else (w in n):
            return True
    return False


def country_verdict(name: str, currency: str | None, key: str, words: list[str]) -> str:
    """Лот конкурента — нужной страны? "yes" / "no" / "unknown" (страна в названии не видна).
    Слова своей страны → да; слова другой страны → нет; иначе по валюте номинала
    («10 TRY» для США — нет, «10$» — да)."""
    if not words:
        return "yes"  # проверка страны выключена («-»)
    if country_match(name, words):
        return "yes"
    for other, (_, other_words, _) in COUNTRIES.items():
        if other not in (key, "any") and other_words and country_match(name, _words(other_words)):
            return "no"
    currencies = COUNTRIES.get(key, ("", "", ()))[2]
    if currency and currencies:
        return "yes" if currency in currencies else "no"
    return "unknown"


async def auto_lot_ref(session: AsyncSession, client, tg: int, sec: CostSection, own_user_id: str) -> str:
    """Лот для сравнения без ссылки: из другого раздела того же бренда, иначе свой активный
    лот с названием бренда. Найденный запоминается в разделе. "" — не нашли."""
    from .automation import norm_lot

    for other in await sections(session, tg, sec.brand):
        if other.id != sec.id and other.lot_ref:
            sec.lot_ref = other.lot_ref
            await session.commit()
            return sec.lot_ref
    words = BRAND_WORDS.get(sec.brand, ())
    if not words or not own_user_id:
        return ""
    items = await client.my_items(own_user_id, limit=96, statuses=["APPROVED"])
    for it in items:
        name = norm_lot(it.name)
        if any(norm_lot(w) in name for w in words):
            sec.lot_ref = it.url[:500]
            await session.commit()
            return sec.lot_ref
    return ""


async def sections(session: AsyncSession, tg: int, brand: str | None = None) -> list[CostSection]:
    q = select(CostSection).where(CostSection.seller_tg_id == tg)
    if brand:
        q = q.where(CostSection.brand == brand)
    return list(await session.scalars(q.order_by(CostSection.brand, CostSection.id)))


async def get_section(session: AsyncSession, tg: int, section_id: int) -> CostSection | None:
    sec = await session.get(CostSection, section_id)
    return sec if sec is not None and sec.seller_tg_id == tg else None


async def get_or_create(session: AsyncSession, tg: int, brand: str, country: str) -> CostSection:
    sec = await session.scalar(
        select(CostSection).where(
            CostSection.seller_tg_id == tg, CostSection.brand == brand, CostSection.country == country
        )
    )
    if sec is None:
        sec = CostSection(
            seller_tg_id=tg, brand=brand, country=country[:64],
            kw=COUNTRIES.get(country, ("", "", ()))[1] if brand != "roblox" else "",
            lot_ref="", fazer_cat="", fazer_name="",
        )
        session.add(sec)
        await session.commit()
    return sec


async def entries(session: AsyncSession, section_id: int) -> list[CostEntry]:
    return list(await session.scalars(
        select(CostEntry).where(CostEntry.section_id == section_id).order_by(CostEntry.nominal)
    ))


async def counts(session: AsyncSession, tg: int) -> dict[int, int]:
    rows = await session.execute(
        select(CostEntry.section_id, func.count(CostEntry.id))
        .where(CostEntry.seller_tg_id == tg).group_by(CostEntry.section_id)
    )
    return {sid: n for sid, n in rows.all()}


async def upsert_costs(session: AsyncSession, sec: CostSection, costs: dict[int, float]) -> None:
    """Добавляет номиналы; цена уже существующего номинала заменяется."""
    have = {e.nominal: e for e in await entries(session, sec.id)}
    for nominal, cost in costs.items():
        if nominal in have:
            have[nominal].cost = cost
        else:
            session.add(CostEntry(section_id=sec.id, seller_tg_id=sec.seller_tg_id, nominal=nominal, cost=cost))
    await session.commit()


async def delete_section(session: AsyncSession, sec: CostSection) -> None:
    await session.execute(delete(CostEntry).where(CostEntry.section_id == sec.id))
    if await ft.get_setting(session, sec.seller_tg_id, ACTIVE_KEY, "") == str(sec.id):
        await ft.set_setting(session, sec.seller_tg_id, ACTIVE_KEY, "")
    await session.delete(sec)
    await session.commit()


async def active(session: AsyncSession, tg: int) -> CostSection | None:
    raw = await ft.get_setting(session, tg, ACTIVE_KEY, "")
    return await get_section(session, tg, int(raw)) if raw.isdigit() else None


async def set_active(session: AsyncSession, tg: int, sec: CostSection) -> None:
    from . import pricing

    if await ft.get_setting(session, tg, ACTIVE_KEY, "") != str(sec.id):
        await ft.set_setting(session, tg, ACTIVE_KEY, str(sec.id))
        await ft.set_setting(session, tg, pricing.TRACK_SEEN_KEY, "")  # другие номиналы — заново


async def migrate_legacy(session: AsyncSession, tg: int) -> CostSection | None:
    """Старый общий список закупки → раздел «Roblox · Без страны» (один раз, если разделов нет)."""
    from . import pricing

    legacy = pricing.parse_costs(await ft.get_setting(session, tg, pricing.NOMINAL_COSTS_KEY, ""))
    if not legacy or await sections(session, tg):
        return None
    sec = await get_or_create(session, tg, "roblox", "any")
    sec.lot_ref = (await ft.get_setting(session, tg, pricing.NOMINAL_LINK_KEY, ""))[:500]
    sec.fazer_cat = await ft.get_setting(session, tg, pricing.FAZER_CAT_KEY, "")
    sec.fazer_name = (await ft.get_setting(session, tg, pricing.FAZER_NAME_KEY, ""))[:64]
    await upsert_costs(session, sec, legacy)
    await set_active(session, tg, sec)
    return sec
