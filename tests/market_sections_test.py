"""📦 Разделы закупки: бренд → страна → номиналы кнопками; расчёт по выбранному разделу
(номинал «5$», только лоты своей страны)."""
import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select, func
from bot.crypto import TokenCipher
from bot.db import init_db, CostEntry, CostSection
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft, market_sections as ms, pricing
from bot.services.sellers import get_or_create_seller
from bot.handlers import market as hm, pricing as hp

APPLE = [  # категория «Apple», лоты разных стран
    {"id": "a1", "slug": "a1", "name": "Apple Gift Card 5$ USA", "price": 520},
    {"id": "a2", "slug": "a2", "name": "🍏 Apple 5 $ Турция", "price": 300},       # дешевле, но другая страна
    {"id": "a3", "slug": "a3", "name": "Apple 10$ (США) моментально", "price": 1000},
    {"id": "a4", "slug": "a4", "name": "Apple Gift Card 25$", "price": 2500},        # страна не указана
    {"id": "a5", "slug": "a5", "name": "Apple 10$ USA", "price": 900, "own": True},  # свой — не считаем
]
for a in APPLE:
    a.update(obtainingType={"id": "code"}, user={"id": "me" if a.pop("own", False) else "u" + a["id"]})

async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": {"id": "ref", "name": "x", "category": {"id": "apple"},
                           "obtainingType": {"id": "code", "name": "Код"}}}}), None)
    assert op == "items", op
    lots = sorted(APPLE, key=lambda l: l["price"])
    return RawResponse(200, json.dumps({"data": {"items": {"edges": [{"node": n} for n in lots], "pageInfo": {}}}}), None)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

USER = SimpleNamespace(id=1, username="u", first_name="u")
OUT = []
class Msg:
    def __init__(self, text=""): self.text = text; self.from_user = USER
    async def answer(self, text, reply_markup=None, **k):
        OUT.append((text, reply_markup)); return SimpleNamespace(edit_text=_noop, delete=_noop)
    async def edit_text(self, text, reply_markup=None, **k): OUT.append((text, reply_markup))
async def _noop(*a, **k): pass
class Cb:
    def __init__(self, data, user=USER): self.data = data; self.from_user = user; self.message = Msg(); self.alerts = []
    async def answer(self, text=None, show_alert=False, **k):
        if show_alert: self.alerts.append(text)

def buttons(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, USER)
        seller.playerok_id = "me"; seller.token_enc = cipher.encrypt("T")
        # старый общий список закупки (как у владельца на скриншоте)
        await ft.set_setting(s, 1, pricing.NOMINAL_COSTS_KEY, "2000 21.24")
        await ft.set_setting(s, 1, pricing.NOMINAL_LINK_KEY, "https://playerok.com/products/robux")
        await s.commit()
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))

    # --- старый список переезжает в раздел «Roblox · Без страны» и становится ★ ---
    await hm.home(Cb("mk:home"), state, sessions)
    text, kb = OUT[-1]
    assert "★ 🧱 Roblox · 🌐 Без страны — 1 номинал" in text, text
    btns = buttons(kb)
    assert {"mk:b:apple", "mk:b:psn", "mk:b:xbox", "mk:b:nintendo", "mk:b:steam", "mk:b:roblox"} <= set(btns), btns
    async with sessions() as s:
        setup = await pricing.market_setup(s, 1)
    assert setup.lot_ref.endswith("/robux") and setup.costs[2000].usd == 21.24 and not setup.currency_first
    await hm.home(Cb("mk:home"), state, sessions)  # повторно не дублирует
    async with sessions() as s:
        assert await s.scalar(select(func.count(CostSection.id))) == 1
    print("1. старая закупка перенесена в раздел Roblox")

    # --- Apple → США: раздел создаётся кнопками, номиналы добавляются списком ---
    await hm.brand(Cb("mk:b:apple"), state, sessions)
    text, kb = OUT[-1]
    assert "mk:c:apple:us" in buttons(kb) and "mk:c:apple:tr" in buttons(kb) and "mk:bc:apple" in buttons(kb)
    await hm.country(Cb("mk:c:apple:us"), sessions)
    text, kb = OUT[-1]
    assert "🍏 Apple · 🇺🇸 США" in text and "пока нет" in text and "usa, сша" in text, text
    async with sessions() as s:
        sec = (await ms.sections(s, 1, "apple"))[0]
    await hm.add_ask(Cb(f"mk:add:{sec.id}"), state)
    await hm.add_save(Msg("5 4.6\n10 9,1\n25$ - 23"), state, sessions)
    text, kb = OUT[-1]
    assert "• 5 → $4.6" in text and "• 10 → $9.1" in text and "• 25 → $23" in text, text
    # ещё раз тот же номинал — цена заменяется, а не дублируется
    await hm.add_ask(Cb(f"mk:add:{sec.id}"), state)
    await hm.add_save(Msg("25 22.5"), state, sessions)
    async with sessions() as s:
        got = {e.nominal: e.cost for e in await ms.entries(s, sec.id)}
    assert got == {5: 4.6, 10: 9.1, 25: 22.5}, got
    # правка цены одного номинала и удаление другого — кнопками
    e5 = next(b for b in buttons(OUT[-1][1]) if b.startswith("mk:e:"))
    await hm.entry_ask(Cb(e5), state, sessions)
    assert "Номинал <b>5</b>" in OUT[-1][0]
    await hm.entry_save(Msg("4.5"), state, sessions)
    edel = next(b for b in buttons(OUT[-1][1]) if b.startswith("mk:e:") and b != e5)  # 10
    await hm.entry_ask(Cb(edel), state, sessions)
    del_btn = buttons(OUT[-1][1])[0]
    await hm.entry_delete(Cb(del_btn), state, sessions)
    async with sessions() as s:
        got = {e.nominal: e.cost for e in await ms.entries(s, sec.id)}
    assert got == {5: 4.5, 25: 22.5}, got
    await hm.add_ask(Cb(f"mk:add:{sec.id}"), state)
    await hm.add_save(Msg("10 9.1"), state, sessions)
    print("2. Apple · США: номиналы добавляются, правятся и удаляются кнопками")

    # --- лот для сравнения, ★, расчёт: только лоты США, номинал «5$» ---
    cb = Cb(f"mk:run:{sec.id}")
    await hm.run(cb, sessions, cipher)
    assert cb.alerts and "Лот для сравнения" in cb.alerts[0]
    await hm.lot_ask(Cb(f"mk:lot:{sec.id}"), state)
    await hm.lot_save(Msg("https://playerok.com/products/apple-ref"), state, sessions)
    await hm.run(Cb(f"mk:run:{sec.id}"), sessions, cipher)
    report = OUT[-1][0]
    print(report)
    assert "Выгода по рынку</b> · 🍏 Apple · 🇺🇸 США" in report, report
    assert "<b>5</b> · 520 ₽ ÷ 104 = <b>$5.00</b>" in report and "300 ₽" not in report, report  # Турция не считается
    assert "<b>10</b> · 1 000 ₽" in report and "900 ₽" not in report, report                   # свой лот не считается
    assert "<b>25</b> · 2 500 ₽" in report and "страна в названиях не указана" in report, report
    assert "закупка $4.5 → прибыль <b>$+0.50</b>" in report, report
    async with sessions() as s:
        assert (await ms.active(s, 1)).id == sec.id  # «Посчитать» делает раздел ★
    # меню расчёта показывает раздел и ведёт кнопки в него
    mtext, mkb = await hp._market_menu(sessions, 1)
    assert "★ 🍏 Apple · 🇺🇸 США — номиналов: 3" in mtext and f"mk:lot:{sec.id}" in buttons(mkb) and "mk:home" in buttons(mkb)
    print("3. расчёт по разделу: свои номиналы, только лоты своей страны")

    # --- страна без проверки ---
    await hm.kw_ask(Cb(f"mk:kw:{sec.id}"), state, sessions)
    await hm.kw_save(Msg("-"), state, sessions)
    async with sessions() as s:
        setup = await pricing.market_setup(s, 1)
    assert setup.country_words == [] and setup.currency_first and sorted(setup.costs) == [5, 10, 25]
    await hm.run(Cb(f"mk:run:{sec.id}"), sessions, cipher)
    assert "<b>5</b> · 300 ₽" in OUT[-1][0]  # без страны — самый дешёвый любой
    print("4. «-» — страну не проверять")

    # --- чужой раздел недоступен; удаление с подтверждением ---
    other = SimpleNamespace(id=2, username="x", first_name="x")
    cb = Cb(f"mk:s:{sec.id}", other)
    await hm.section(cb, state, sessions)
    assert cb.alerts == ["Раздел удалён"]
    await hm.delete_ask(Cb(f"mk:del:{sec.id}"), sessions)
    assert buttons(OUT[-1][1]) == [f"mk:delok:{sec.id}", f"mk:s:{sec.id}"]
    await hm.delete_ok(Cb(f"mk:delok:{sec.id}"), sessions)
    async with sessions() as s:
        assert await s.scalar(select(func.count(CostEntry.id)).where(CostEntry.section_id == sec.id)) == 0
        assert await ms.active(s, 1) is None
    print("5. чужие разделы недоступны; удаление — с вопросом")

    # --- все кнопки укладываются в лимит Telegram ---
    for scr in (await hm.home_screen(sessions, 1), await hm.brand_screen(sessions, 1, "nintendo")):
        assert all(len(d.encode()) <= 64 for d in buttons(scr[1]))
    print("OK")
asyncio.run(main())
