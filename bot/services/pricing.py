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
import re as _re_mod
from dataclasses import dataclass, field
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


NAME_KW = "*"  # competitor_kw: сравнивать с лотами, похожими на название своего лота


def name_match(my_name: str, other: str) -> bool:
    """Чужой лот похож на свой по названию. Числа (номинал) — все и целым словом
    (100 ≠ 1000); если чисел нет — совпадает хотя бы половина слов (по началу слова).
    Категория и способ получения уже отфильтрованы запросом."""
    tokens = _norm(my_name).split()
    nums = [w for w in tokens if w.isdigit()]
    words = [w[:5] for w in tokens if not w.isdigit() and len(w) >= 3]
    o = _norm(other)
    o_tokens = o.split()
    if any(n not in o_tokens for n in nums):
        return False
    if nums:
        return True
    if not words:
        return False
    return sum(w in o for w in words) * 2 >= len(words)


def kw_label(rule: PriceRule) -> str:
    if rule.competitor_kw == NAME_KW:
        return "похожими по названию моего лота"
    return f"«{rule.competitor_kw}»"


def find_my_lot(items: list[Item], rule: PriceRule) -> Item | None:
    key = rule.lot_key.strip()
    # По названию — только обычные лоты: премиум-лотам Playerok не даёт менять цену.
    by_name = [it for it in items if not it.is_premium]
    for it in items:
        if key in (it.id, it.slug) or (it.slug and key.rstrip("/").endswith("/" + it.slug)):
            return it
    target = _norm(key)
    exact = [it for it in by_name if _norm(it.name) == target]
    if exact:
        return exact[0]
    loose = [it for it in by_name if kw_match(key, it.name)]
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
    if item.is_premium:
        return "лот с премиум-статусом — Playerok не даёт менять ему цену", False
    price, raw = item.price, item.raw_price
    if not isinstance(price, (int, float)) or not isinstance(raw, (int, float)) or price <= 0 or raw <= 0:
        return "Playerok не отдал цену лота", False
    if not item.category_id:
        return "Playerok не отдал категорию лота", False
    # Только тот же способ получения: «робуксы кодом» не сравниваем с «по нику».
    others = await client.category_items(item.category_id, obtaining_type_id=item.obtaining_type_id or None)
    rivals = [
        o for o in others
        if o.id != item.id
        and (not item.obtaining_type_id or not o.obtaining_type_id or o.obtaining_type_id == item.obtaining_type_id)
        and o.user_id != (seller.playerok_id or "")
        and isinstance(o.price, (int, float)) and o.price > 0
        and (name_match(item.name or mine.name, o.name) if rule.competitor_kw == NAME_KW
             else kw_match(rule.competitor_kw, o.name))
    ]
    way = f" ({item.obtaining_type_name})" if item.obtaining_type_name else ""
    if not rivals:
        return f"конкурентов ({kw_label(rule)}){way} не найдено, цена {_rub(price)}", False
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
    try:
        answer = await client.update_item_price(item.id, new_raw)
    except AuthRequired:
        raise
    except PlayerokError as e:
        return f"{price_error(str(e))}; моя {_rub(price)}, у конкурента {_rub(best.price)} — {_who(best)}", False
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


# ===================== разовое снижение цены своих лотов =====================

MAX_CUT_LOTS = 30


def select_my_lots(items: list[Item], query: str) -> list[Item]:
    """Свои лоты по ссылке, точному названию или ключевым словам."""
    q = query.strip()
    if "/products/" in q:
        slug = q.split("/products/", 1)[1].split("?")[0].strip("/")
        return [it for it in items if slug in (it.slug, it.id)]
    exact = [it for it in items if _norm(it.name) == _norm(q)]
    return exact or [it for it in items if kw_match(q, it.name)]


MAX_DROP = 0.5  # за раз — не дешевле половины текущей цены (защита от опечаток)


@dataclass(frozen=True)
class Cut:
    kind: str  # set — поставить N ₽, rub — снизить на N ₽, pct — снизить на N %
    value: float

    def target(self, price: float) -> float:
        if self.kind == "pct":
            return price * (1 - self.value / 100)
        if self.kind == "set":
            return self.value
        return price - self.value

    def skip_reason(self, price: float) -> str | None:
        """Почему этот лот не трогаем (None — снижаем)."""
        target = round(self.target(price), 2)
        if target >= price:
            return "не дороже — не трогаю"
        if target < price * MAX_DROP:
            return f"не трогаю: {_rub(target)} — дешевле вдвое, похоже на опечатку"
        return None

    def label(self) -> str:
        if self.kind == "pct":
            return f"на {self.value:g}%"
        if self.kind == "set":
            return f"до {_rub(self.value)}"
        return f"на {_rub(self.value)}"


def parse_cut(text: str) -> Cut | None:
    """«682» / «=682» / «до 682» — поставить 682 ₽; «-10» / «на 10» — снизить на 10 ₽;
    «-5%» / «5%» — снизить на 5 %."""
    s = text.strip().lower().replace(",", ".").replace("−", "-").replace("–", "-")
    s = s.replace("руб", "").replace("₽", "").replace("р.", "").rstrip("р").replace(" ", "")
    kind = "set"
    if s.endswith("%"):
        kind, s = "pct", s[:-1].lstrip("-").removeprefix("на")
    elif s.startswith("-"):
        kind, s = "rub", s[1:]
    elif s.startswith("на"):
        kind, s = "rub", s[2:].lstrip("-")
    elif s.startswith("до"):
        s = s[2:]
    s = s.lstrip("=")
    try:
        v = float(s)
    except ValueError:
        return None
    if v <= 0 or (kind == "pct" and v >= 100):
        return None
    return Cut(kind, v)


_MIN_PRICE_RE = _re_mod.compile(r"minimal price\D*(\d+(?:[.,]\d+)?)", _re_mod.I)
_AT_LEAST_RE = _re_mod.compile(r"price must be at least\D*(\d+(?:[.,]\d+)?)", _re_mod.I)
_MIN_DISCOUNT_RE = _re_mod.compile(r"minimal discount[^\d\[]*(\d+(?:[.,]\d+)?)?\s*(%)?", _re_mod.I)


def price_error(text: str) -> str:
    """Ошибки Playerok о цене — по-русски."""
    m = _MIN_PRICE_RE.search(text) or _AT_LEAST_RE.search(text)
    if m:
        return f"Playerok не даёт цену ниже {m.group(1)} ₽ для этого лота"
    m = _MIN_DISCOUNT_RE.search(text)
    if m:
        size = f"{m.group(1)}{'%' if m.group(2) else ''}" if m.group(1) else "размер Playerok не сообщил"
        details = f" Подробности от Playerok: {text[text.index('['):]}" if "[" in text else ""
        return (f"Playerok не даёт снизить цену так мало — нужна скидка больше (минимальная скидка: {size}). "
                f"Снизь сильнее.{details}")
    return f"ошибка: {text[:300]}"


async def cut_price(client: PlayerokClient, item_id: str, cut: Cut) -> tuple[str, bool]:
    """Снижает цену одного своего лота. (пометка, изменили ли цену). Цены — для покупателя."""
    item = await client.get_item(item_id)
    price, raw = item.price, item.raw_price
    if item.is_premium:
        return f"{_rub(price or 0)} — премиум-лот, Playerok не даёт менять ему цену, не трогаю", False
    if not isinstance(price, (int, float)) or not isinstance(raw, (int, float)) or price <= 0 or raw <= 0:
        return "Playerok не отдал цену лота", False
    why = cut.skip_reason(price)
    if why:
        return f"{_rub(price)} — {why}", False
    target = round(cut.target(price), 2)
    k = price / raw  # множитель комиссии площадки
    new_raw = max(1, math.floor(target / k + 1e-6))  # 1e-6 — от ошибок округления (99/1.1 = 89.999…)
    if new_raw >= raw:
        return f"{_rub(price)} — снижать некуда", False
    try:
        answer = await client.update_item_price(item.id, new_raw)
    except AuthRequired:
        raise
    except PlayerokError as e:
        return f"{_rub(price)} → {_rub(target)}: {price_error(str(e))}", False
    after = answer.get("price")
    if not isinstance(after, (int, float)):
        after = (await client.get_item(item.id)).price
    status = str(answer.get("status") or "")
    if not isinstance(after, (int, float)) or after >= price:
        return f"Playerok принял запрос, но цена осталась {_rub(price)} (статус {status or '?'})", False
    note = f"{_rub(price)} → {_rub(after)}"
    if status and status.upper() not in ("APPROVED", "ACTIVE"):
        note += f" (статус лота: {status})"
    return note, True


# ===================== цены по номиналам =====================

import re as _re

NOMINAL_RE = _re.compile(r"(?<![\d.,])(\d{2,6})(?![\d.,])")
# Валюты номиналов подарочных карт: код → как пишут в названиях лотов.
CURRENCIES = {
    "USD": r"\$|usd|долл\w*",
    "EUR": r"€|eur|евро",
    "GBP": r"£|gbp|фунт\w*",
    "TRY": r"₺|try|tl|лир\w*",
    "INR": r"₹|inr|руп\w*",
    "PLN": r"zł|pln|злот\w*",
    "AED": r"aed|дирх\w*",
    "SAR": r"sar|риал\w*",
    "BRL": r"brl|реал\w*",
    "JPY": r"¥|jpy|yen|иен\w*",
    "KZT": r"₸|kzt|тенге",
    "UAH": r"uah|грив\w*",
    "ARS": r"ars|песо",
    "RUB": r"₽|rub|руб\w*",
}
_CUR_CODES = [(code, _re.compile(rx, _re.I)) for code, rx in CURRENCIES.items()]
_CUR = "(" + "|".join(CURRENCIES.values()) + ")"
_NOM_CUR_RE = _re.compile(r"(?<![\d.,])(\d{1,6})\s*" + _CUR + r"(?![a-zа-я])", _re.I)
_CUR_NOM_RE = _re.compile(r"(?<![a-zа-я])" + _CUR + r"\s*(\d{1,6})(?![\d.,])", _re.I)


def currency_code(token: str) -> str | None:
    return next((code for code, rx in _CUR_CODES if rx.fullmatch(token.strip())), None)


def nominal_cur(name: str, currency_first: bool = False) -> tuple[int | None, str | None]:
    """(номинал, валюта). currency_first — для подарочных карт: число рядом с валютой
    («Apple 5$ USA» → 5 USD, «PSN 100 TL» → 100 TRY), иначе первое число 2–6 цифр."""
    if currency_first:
        m = _NOM_CUR_RE.search(name or "")
        if m:
            return int(m.group(1)), currency_code(m.group(2))
        m = _CUR_NOM_RE.search(name or "")
        if m:
            return int(m.group(2)), currency_code(m.group(1))
    m = NOMINAL_RE.search(name or "")
    return (int(m.group(1)) if m else None), None


def nominal_of(name: str, currency_first: bool = False) -> int | None:
    """Номинал из названия: первое число 2–6 цифр («✅ДЛЯ РФ✅100 РОБУКСОВ» → 100);
    с currency_first — сначала число рядом с валютой (см. nominal_cur)."""
    return nominal_cur(name, currency_first)[0]


def parse_costs(text: str) -> dict[int, float]:
    """«100 0.95», «100=0,95», «500 - 4.6$» по строкам → {номинал: закупка $}."""
    costs: dict[int, float] = {}
    for line in (text or "").splitlines():
        nums = _re.findall(r"\d+(?:[.,]\d+)?", line)
        if len(nums) >= 2:
            try:
                costs[int(float(nums[0].replace(",", ".")))] = float(nums[1].replace(",", "."))
            except ValueError:
                continue
    return costs


# ----- настройки калькулятора «Выгода по рынку» и трекера -----
NOMINAL_LINK_KEY = "nominal_lot_ref"
NOMINAL_DIV_KEY = "nominal_divisor"
NOMINAL_COSTS_KEY = "nominal_costs"          # закупка вручную: «100 0.95» по строкам
FAZER_CAT_KEY = "nominal_fazer_cat"          # категория FazerCards — закупка оттуда
FAZER_NAME_KEY = "nominal_fazer_name"
TRACK_KEY = "market_track"                   # трекер выгодных номиналов вкл/выкл
TRACK_MIN_KEY = "market_track_min"           # минимальная прибыль $ для уведомления
TRACK_LAST_KEY = "market_track_last"
TRACK_SEEN_KEY = "market_track_seen"         # номиналы, о которых уже уведомили
TRACK_EVERY = timedelta(minutes=30)
DEEP_PAGES = 5  # сколько ещё страниц листать, если дешёвые лоты номинала — другой страны


@dataclass
class Cost:
    usd: float
    source: str  # "fazer" | "manual"
    stock: int | None = None


@dataclass
class MarketScan:
    best: dict[int, Item]
    scanned: int
    partial: bool
    way: str
    loose: set[int] = field(default_factory=set)  # номиналы, где страна в названиях не нашлась
    absent: set[int] = field(default_factory=set)  # номиналы, где лотов нужной страны нет


@dataclass
class MarketSetup:
    """С чем сравнивать: лот (категория), закупка, слова страны — из активного раздела
    или старых общих настроек."""
    lot_ref: str
    costs: dict[int, Cost]
    note: str | None = None
    country_words: list[str] = field(default_factory=list)
    currency_first: bool = False
    title: str = ""
    section_id: int | None = None
    country_key: str = ""


@dataclass
class MarketRow:
    nominal: int
    lot: Item
    usd: float
    cost: Cost | None
    profit: float | None


async def get_divisor(session, tg: int) -> float:
    try:
        return float(await ft.get_setting(session, tg, NOMINAL_DIV_KEY, "104")) or 104.0
    except ValueError:
        return 104.0


async def _fazer_costs(session, tg: int, cat: str, currency_first: bool) -> tuple[dict[int, Cost], str | None]:
    """Цены FazerCards по номиналам выбранной категории — только админу (ключ владельца)."""
    from ..plugins.access import is_admin
    from ..plugins.giftcard import service as gc
    from ..plugins.giftcard.client import FazerError

    if not cat or not is_admin(tg):
        return {}, None
    key = await gc.get_api_key(session, tg)
    if not key:
        return {}, "FazerCards: не задан API-ключ (/giftcard → 🔑)"
    try:
        async with gc.make_client(key) as api:
            data = await api.offers(cat)
    except FazerError as e:
        return {}, f"FazerCards: {e}"
    costs: dict[int, Cost] = {}
    for o in data.get("offers") or []:
        n = (nominal_of(str(o.get("name") or ""), currency_first)
             or nominal_of(str(o.get("card_id") or "").replace("_", " "), currency_first))
        try:
            price = float(o.get("price_usd"))
        except (TypeError, ValueError):
            continue
        if n is not None:
            stock = o.get("stock") if isinstance(o.get("stock"), int) else None
            costs[n] = Cost(price, "fazer", stock)
    return costs, None


async def _approute_costs(session, tg: int, product_id: str) -> tuple[dict[int, Cost], str | None]:
    """Цены AppRoute по номиналам товара (items[].nominal → price) — только админу (ключ владельца)."""
    from ..plugins.access import is_admin
    from ..plugins.giftcard import service as gc
    from ..plugins.giftcard.client import FazerError

    if not product_id or not is_admin(tg):
        return {}, None
    if not await gc.get_api_key(session, tg, "approute"):
        return {}, "AppRoute: не задан API-ключ (/giftcard → 🔑 Ключ AppRoute)"
    try:
        async with await gc.make_approute(session, tg) as api:
            product = await api.service(product_id)
    except FazerError as e:
        return {}, str(e)
    costs: dict[int, Cost] = {}
    for item in product.get("items") or []:
        try:
            nominal, price = float(item.get("nominal")), float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if nominal != int(nominal):
            continue
        stock = item.get("stock") if isinstance(item.get("stock"), int) else (0 if item.get("available") is False else None)
        costs[int(nominal)] = Cost(price, "approute", stock)
    return costs, None


def _cheapest(*sources: dict[int, Cost]) -> dict[int, Cost]:
    """По каждому номиналу — самый дешёвый поставщик, у которого товар есть в наличии."""
    best: dict[int, Cost] = {}
    for src in sources:
        for n, c in src.items():
            cur = best.get(n)
            out = c.stock is not None and c.stock <= 0
            cur_out = cur is not None and cur.stock is not None and cur.stock <= 0
            if cur is None or (cur_out and not out) or (out == cur_out and c.usd < cur.usd):
                best[n] = c
    return best


async def market_setup(session, tg: int) -> MarketSetup:
    """Активный раздел закупки (бренд + страна) или старые общие настройки."""
    from . import market_sections as ms

    sec = await ms.active(session, tg)
    if sec is None:
        costs = {n: Cost(v, "manual") for n, v in parse_costs(await ft.get_setting(session, tg, NOMINAL_COSTS_KEY, "")).items()}
        fazer, note = await _fazer_costs(session, tg, await ft.get_setting(session, tg, FAZER_CAT_KEY, ""), False)
        costs.update(fazer)  # FazerCards главнее ручной
        return MarketSetup(await ft.get_setting(session, tg, NOMINAL_LINK_KEY, ""), costs, note)
    currency_first = sec.brand != "roblox"
    costs = {e.nominal: Cost(e.cost, "manual") for e in await ms.entries(session, sec.id)}
    fazer, note = await _fazer_costs(session, tg, sec.fazer_cat, currency_first)
    approute, ar_note = await _approute_costs(session, tg, sec.ar_product or "")
    costs.update(_cheapest(fazer, approute))  # поставщики главнее ручной цены, из них — дешевле
    note = "; ".join(x for x in (note, ar_note) if x) or None
    return MarketSetup(sec.lot_ref, costs, note, ms.kw_list(sec), currency_first, ms.section_title(sec), sec.id,
                       sec.country)


async def load_costs(session, tg: int) -> tuple[dict[int, Cost], str | None]:
    """Закупка по номиналам: вручную + цены FazerCards (они главнее, если выбрана категория)."""
    setup = await market_setup(session, tg)
    return setup.costs, setup.note


async def market_scan(
    client: PlayerokClient, lot_ref: str, pages: int = 5, own_user_id: str | None = None, on_page=None,
    targets: list[int] | None = None, on_step=None, country_words: list[str] | None = None,
    currency_first: bool = False, country_key: str = "",
) -> MarketScan:
    """Самый дешёвый ЧУЖОЙ лот каждого номинала в категории (и способе получения) лота lot_ref.

    targets — номиналы, которые нужны (из закупки): для каждого отдельный поиск по числу
    с сортировкой «сначала дешёвые» — так находится настоящий минимум, а не минимум среди
    первых страниц каталога. Если Playerok не сортирует — листаем выдачу поиска до 8 страниц.
    Без targets (или если поиск ничего не дал) — просмотр каталога подряд."""
    ref = lot_ref.strip()
    slug = ref.split("/products/", 1)[1].split("?")[0].strip("/") if "/products/" in ref else ref
    try:
        item = await client.get_item(slug=slug)
    except PlayerokError as e:
        raise PlayerokError(f"лот по ссылке: {e}") from e
    if not item.category_id:
        raise PlayerokError("Playerok не отдал категорию этого лота")
    way_id = item.obtaining_type_id or None
    filt_way = [way_id]  # фильтр по способу получения на стороне Playerok (можно отключить)

    def suitable(o: Item) -> bool:
        return (
            (not way_id or not o.obtaining_type_id or o.obtaining_type_id == way_id)
            and (not own_user_id or o.user_id != own_user_id)  # только чужие
            and isinstance(o.price, (int, float)) and o.price > 0
        )

    async def fetch(**kw) -> list[Item]:
        kw.setdefault("cache", True)  # повторный расчёт за 15 мин — без запросов к Playerok
        try:
            return await client.category_items(item.category_id, obtaining_type_id=filt_way[0], **kw)
        except PlayerokError as e:
            if not filt_way[0]:
                raise
            # Фильтр по способу получения мог не понравиться серверу — без него, фильтруем сами.
            log.info("выгода по рынку: без фильтра obtainingTypeId после ошибки: %s", e)
            filt_way[0] = None
            return await client.category_items(item.category_id, **kw)

    from .market_sections import country_verdict

    def choose(n: int, same: list[Item]) -> bool:
        """Самый дешёвый лот номинала своей страны. Лоты другой страны (по словам или
        валюте: «10 TRY» — не США) не берём никогда; без страны в названии — только если
        своих нет (строка помечается). True — нашли лот своей страны."""
        yes, unknown = [], []
        for o in same:
            v = country_verdict(o.name, nominal_cur(o.name, currency_first)[1], country_key, country_words or [])
            (yes if v == "yes" else unknown if v == "unknown" else []).append(o)
        if yes:
            best[n] = min(yes, key=lambda o: o.price)
            loose.discard(n)
            return True
        if unknown:
            best[n] = min(unknown, key=lambda o: o.price)
            loose.add(n)
        return False

    best: dict[int, Item] = {}
    loose: set[int] = set()
    filtering = bool(country_words)
    scanned = 0
    partial = False
    hits = 0
    if targets:
        per_nominal_pages = 1
        # С самого большого номинала: по нему надёжнее видно, сработал ли поиск
        # («50» есть и в «500», и в «4500», а «4500» в случайной выдаче встречается редко).
        targets = sorted(targets, reverse=True)
        for i, n in enumerate(targets[:20], 1):
            if on_step is not None:
                await on_step(f"номинал {n} ({i}/{min(len(targets), 20)})")
            try:
                lots = await fetch(pages=per_nominal_pages, search=str(n), sort_price=True)
                if i == 1 and len(lots) >= 6 and sum(str(n) in o.name for o in lots) < len(lots) / 2:
                    # Поиск по тексту не применился (в выдаче что попало) — смотрим каталог подряд.
                    log.info("выгода по рынку: поиск по номиналу не работает — просмотр каталога")
                    hits = 0
                    break
                if per_nominal_pages == 1 and not client.last_sorted and len(lots) >= 24:
                    # Playerok не отсортировал — листаем выдачу поиска до конца (до 8 страниц).
                    per_nominal_pages = 8
                    lots = await fetch(pages=8, search=str(n), sort_price=True)
            except PlayerokError as e:
                if i == 1:
                    raise PlayerokError(f"лоты категории: {e}") from e
                partial = True  # лимит/сбой посреди поиска — показываем найденное
                break
            partial = partial or getattr(client, "partial", False)
            scanned += len(lots)
            same = [o for o in lots if suitable(o) and nominal_of(o.name, currency_first) == n]
            if same:
                hits += 1
            found = choose(n, same) if same else False
            next_page = client.last_after
            if filtering and not found and per_nominal_pages == 1 and next_page:
                # Дешёвые лоты номинала — другой страны (10 TRY дешевле 10 $): листаем дальше
                # с того же места и останавливаемся на первой странице, где есть лот своей страны
                # (выдача по возрастанию цены — он и самый дешёвый).
                def own_country(page: list[Item], n=n) -> bool:
                    return any(
                        suitable(o) and nominal_cur(o.name, currency_first)[0] == n
                        and country_verdict(o.name, nominal_cur(o.name, currency_first)[1], country_key,
                                            country_words or []) == "yes"
                        for o in page
                    )

                async def deep_page(page: int, pages: int, count: int, n=n) -> None:
                    if on_step is not None:
                        await on_step(f"номинал {n}: ищу лоты своей страны, страница {page + 1}")

                try:
                    more = await fetch(pages=DEEP_PAGES, search=str(n), sort_price=True, after=next_page,
                                       until=own_country, on_page=deep_page)
                except PlayerokError:
                    partial = True
                    more = []
                scanned += len(more)
                same += [o for o in more if suitable(o) and nominal_of(o.name, currency_first) == n]
                if same:
                    choose(n, same)
    if not targets or hits == 0:
        try:
            lots = await fetch(pages=max(pages, 15) if targets else pages, on_page=on_page, sort_price=True)
        except PlayerokError as e:
            raise PlayerokError(f"лоты категории: {e}") from e
        partial = getattr(client, "partial", False)
        lots = [o for o in lots if suitable(o)]
        scanned = len(lots)
        by_nominal: dict[int, list[Item]] = {}
        for o in lots:
            n = nominal_of(o.name, currency_first)
            if n is not None:
                by_nominal.setdefault(n, []).append(o)
        for n, same in by_nominal.items():
            choose(n, same)
    way = f" · {html.escape(item.obtaining_type_name)}" if item.obtaining_type_name else ""
    absent = {n for n in (targets or []) if n not in best}
    return MarketScan(best, scanned, partial, way, loose, absent)


def market_rows(scan: MarketScan, divisor: float, costs: dict[int, Cost]) -> list[MarketRow]:
    rows = []
    for n in sorted(scan.best):
        lot = scan.best[n]
        usd = lot.price / divisor
        cost = costs.get(n)
        rows.append(MarketRow(n, lot, usd, cost, usd - cost.usd if cost else None))
    return rows


def _cost_label(c: Cost) -> str:
    if c.source in ("fazer", "approute"):
        stock = "" if c.stock is None else (" (нет в наличии)" if c.stock <= 0 else f" (в наличии {c.stock})")
        return f"{'FazerCards' if c.source == 'fazer' else 'AppRoute'} ${c.usd:g}{stock}"
    return f"закупка ${c.usd:g}"


def render_market(
    scan: MarketScan, divisor: float, costs: dict[int, Cost], note: str | None = None, title: str = "",
) -> str:
    lines = [
        f"🧮 <b>Выгода по рынку</b>{' · ' + html.escape(title) if title else ''}{scan.way}",
        f"просмотрено лотов: {scan.scanned}, курс ÷{divisor:g}"
        + (" — <b>частично</b>: Playerok ограничил запросы, нажми «🔄» позже" if scan.partial else ""),
        "<i>самая низкая цена конкурента ÷ курс − закупка = прибыль с продажи</i>",
    ]
    if note:
        lines.append(f"⚠️ {html.escape(note)}")
    lines.append("")
    rows = market_rows(scan, divisor, costs)
    if not rows:
        lines.append("Не нашёл чужих лотов с номиналом в названии.")
    for r in rows:
        line = (f"<b>{r.nominal}</b> · {_rub(r.lot.price)} ÷ {divisor:g} = <b>${r.usd:.2f}</b> — "
                f'<a href="{r.lot.url}">лот</a>')
        if r.nominal in scan.loose:
            line += " <i>(в названии страна не указана — проверь лот)</i>"
        if r.cost is not None:
            margin = f" ({r.profit / r.cost.usd * 100:+.0f}%)" if r.cost.usd > 0 else ""
            line += (f"\n   {_cost_label(r.cost)} → прибыль <b>${r.profit:+.2f}</b>{margin} "
                     f"{'✅' if r.profit > 0 else '❌'}")
        lines.append(line)
    if scan.absent:
        lines += ["", f"Нет лотов конкурентов{' этой страны' if title else ''} для: "
                      f"{', '.join(map(str, sorted(scan.absent)))}."]
    missing = [r.nominal for r in rows if r.cost is None]
    if missing:
        lines += ["", f"Закупка не задана для: {', '.join(map(str, missing[:20]))} — «📦 Разделы закупки» или «🎁 FazerCards»."]
    return "\n".join(lines)


async def nominal_report(
    client: PlayerokClient, lot_ref: str, divisor: float, pages: int = 5,
    costs: dict | None = None, own_user_id: str | None = None,
    on_page=None, cost_note: str | None = None, on_step=None,
    country_words: list[str] | None = None, currency_first: bool = False, title: str = "",
    country_key: str = "",
) -> str:
    """Калькулятор выгоды по рынку (отчёт целиком). costs — {номинал: Cost} или {номинал: $}."""
    norm = {n: (c if isinstance(c, Cost) else Cost(float(c), "manual")) for n, c in (costs or {}).items()}
    scan = await market_scan(client, lot_ref, pages=pages, own_user_id=own_user_id, on_page=on_page,
                             targets=sorted(norm) or None, on_step=on_step,
                             country_words=country_words, currency_first=currency_first,
                             country_key=country_key)
    return render_market(scan, divisor, norm, cost_note, title)


async def track_market(
    bot: Bot | None, sessions: SessionFactory, seller: Seller, client: PlayerokClient, *, force: bool = False,
) -> list[MarketRow] | None:
    """Трекер: раз в 30 мин пересчитывает выгоду и уведомляет, когда номинал СТАЛ выгодным
    (прибыль ≥ порога). Повторно о том же номинале — только если он выпадал из выгодных."""
    tg = seller.tg_id
    async with sessions() as session:
        if not force and await ft.get_setting(session, tg, TRACK_KEY, "0") != "1":
            return None
        now = datetime.utcnow()
        if not force:
            raw = await ft.get_setting(session, tg, TRACK_LAST_KEY, "")
            try:
                last = datetime.fromisoformat(raw) if raw else None
            except ValueError:
                last = None
            if last and now - last < TRACK_EVERY:
                return None
        # Отметку ставим сразу: даже если ниже что-то не задано, не дёргаем API каждые 30 с.
        await ft.set_setting(session, tg, TRACK_LAST_KEY, now.isoformat())
        setup = await market_setup(session, tg)
        if not setup.lot_ref and setup.section_id:
            from . import market_sections as ms

            sec = await ms.get_section(session, tg, setup.section_id)
            if sec is not None:
                setup.lot_ref = await ms.auto_lot_ref(session, client, tg, sec, seller.playerok_id or "")
        if not setup.lot_ref:
            return None
        divisor = await get_divisor(session, tg)
        try:
            min_profit = float(await ft.get_setting(session, tg, TRACK_MIN_KEY, "0.01"))
        except ValueError:
            min_profit = 0.01
        costs = setup.costs
        seen_raw = await ft.get_setting(session, tg, TRACK_SEEN_KEY, "")
    if not costs:
        return None
    # Сканирование Playerok — вне сессии БД (может идти до минуты из-за лимита запросов).
    scan = await market_scan(client, setup.lot_ref, pages=5, own_user_id=seller.playerok_id, targets=sorted(costs),
                             country_words=setup.country_words, currency_first=setup.currency_first,
                             country_key=setup.country_key)
    rows = market_rows(scan, divisor, costs)
    good = [r for r in rows if r.profit is not None and r.profit >= min_profit
            and not (r.cost and r.cost.source in ("fazer", "approute") and r.cost.stock == 0)]
    seen = {int(x) for x in seen_raw.split(",") if x.strip().isdigit()}
    new = [r for r in good if r.nominal not in seen]
    log.info("%s трекер рынка: выгодных %s, новых %s", tag(tg), len(good), len(new))
    async with sessions() as session:
        await ft.set_setting(session, tg, TRACK_SEEN_KEY, ",".join(str(r.nominal) for r in good))
        if new and bot is not None:
            where = f" · {html.escape(setup.title)}" if setup.title else ""
            lines = [f"🔔 <b>Стало выгодно продавать</b>{where} (прибыль ≥ ${min_profit:g}):", ""]
            for r in new:
                lines.append(
                    f"<b>{r.nominal}</b>: на Playerok от {_rub(r.lot.price)} (${r.usd:.2f}), "
                    f"{_cost_label(r.cost)} → <b>${r.profit:+.2f}</b> — "
                    f'<a href="{r.lot.url}">лот</a>'
                )
            await notify(bot, session, tg, "system", "\n".join(lines))
    return good
