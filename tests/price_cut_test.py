"""Снижение цены: по названию своего лота (без ключевых слов) и разово — свои лоты по словам/названию/ссылке."""
import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from bot.crypto import TokenCipher
from bot.db import init_db, PriceRule
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft, pricing
from bot.services.sellers import get_or_create_seller
from bot.handlers import pricing as hp

# мои лоты: цена для покупателя = rawPrice × 1.1
MINE = {
    "my1": {"id": "my1", "slug": "my-100", "name": "💰 100 РОБУКСОВ | ПРОМОКОД", "price": 110, "rawPrice": 100},
    "my2": {"id": "my2", "slug": "my-400", "name": "400 робуксов промокод", "price": 330, "rawPrice": 300},
    "my3": {"id": "my3", "slug": "steam", "name": "Ключ Steam Random", "price": 55, "rawPrice": 50},
}
for m in MINE.values():
    m.update(status="APPROVED", category={"id": "cat"}, user={"id": "me"}, obtainingType={"id": "ot-code", "name": "Промокод"})
RIVALS = []
UPDATES = []
PRICE_IN_ANSWER = {"on": False}
MIN_RAW = {"v": 0}
MIN_DISCOUNT = {"on": False}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "items":
        f = v["filter"]
        if f.get("userId") == "me":
            return RawResponse(200, json.dumps({"data": {"items": {"edges": [{"node": n} for n in MINE.values()], "pageInfo": {}}}}), None)
        return RawResponse(200, json.dumps({"data": {"items": {"edges": [{"node": n} for n in RIVALS], "pageInfo": {}}}}), None)
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": MINE[v["id"]]}}), None)
    if op == "updateItem":
        cur = MINE[v["input"]["id"]]["rawPrice"]
        if MIN_DISCOUNT["on"] and v["input"]["price"] > cur * 0.95:  # как у Playerok: «Minimal discount -»
            return RawResponse(200, json.dumps({"errors": [{"message": "Minimal discount - ",
                "extensions": {"code": "BAD_USER_INPUT", "minDiscount": 5}}]}), None)
        if v["input"]["price"] < MIN_RAW["v"]:
            return RawResponse(200, json.dumps({"errors": [{"message": f"Minimal price - {MIN_RAW['v']}"}]}), None)
        UPDATES.append(v["input"])
        m = MINE[v["input"]["id"]]
        m["rawPrice"] = v["input"]["price"]; m["price"] = round(v["input"]["price"] * 1.1, 2)
        ans = {"id": m["id"], "status": "APPROVED"}
        if PRICE_IN_ANSWER["on"]:
            ans.update(price=m["price"], rawPrice=m["rawPrice"])
        return RawResponse(200, json.dumps({"data": {"updateItem": ans}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

def rival(i, name, price, user="u2"):
    return {"id": f"r{i}", "name": name, "price": price, "status": "APPROVED", "user": {"id": user},
            "obtainingType": {"id": "ot-code"}}

OUT = []
USER = SimpleNamespace(id=1, username="u", first_name="u")
class Msg:
    def __init__(self, text=""): self.text = text; self.from_user = USER
    async def answer(self, text, reply_markup=None, **k):
        OUT.append((text, reply_markup)); return Msg()
    async def edit_text(self, text, reply_markup=None, **k): OUT.append((text, reply_markup))
    async def edit_reply_markup(self, reply_markup=None): pass
class Cb:
    def __init__(self, data): self.data = data; self.from_user = USER; self.message = Msg(); self.alerts = []
    async def answer(self, text=None, show_alert=False, **k):
        if show_alert: self.alerts.append(text)

def buttons(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]

async def main():
    # --- похожие по названию ---
    nm = pricing.name_match
    assert nm("💰 100 РОБУКСОВ | ПРОМОКОД", "100 Robux | промокод")
    assert nm("💰 100 РОБУКСОВ | ПРОМОКОД", "Roblox 100 robux")  # номинал совпал — этого достаточно
    assert not nm("💰 100 РОБУКСОВ | ПРОМОКОД", "1000 робуксов")
    assert not nm("💰 100 РОБУКСОВ | ПРОМОКОД", "Аккаунт роблокс")
    assert nm("Ключ Steam Random", "Random Steam key") and not nm("Ключ Steam Random", "Аккаунт Fortnite")
    print("1. похожесть по названию")

    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, USER)
        seller.playerok_id = "me"; seller.token_enc = cipher.encrypt("T")
        await s.commit()

    # --- правило без ключевых слов: кнопка «🏷 По названию моего лота» ---
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await hp.add_start(Cb("dp:add"), state)
    await hp.add_lot(Msg("https://playerok.com/products/my-100"), state)
    assert buttons(OUT[-1][1]) == ["dp:byname"], OUT[-1]
    await hp.add_competitor_by_name(Cb("dp:byname"), state)
    assert await state.get_state() == hp.AddPriceRule.step.state
    await hp.add_step(Msg("1"), state)
    await hp.add_minimum(Msg("90"), state, sessions)
    async with sessions() as s:
        rule = await s.scalar(select(PriceRule))
        assert rule.competitor_kw == pricing.NAME_KW and rule.lot_key == "my-100"
    text, kb = await hp.render_dumping(sessions, seller, ft.FEATURE_BY_KEY["dumping"])
    assert "похожими по названию моего лота" in text and "dp:cut" in buttons(kb), text
    RIVALS[:] = [rival(1, "100 Robux промокод", 105), rival(2, "1000 robux", 50),
                 rival(3, "100 робуксов", 80, user="me"), rival(4, "Аккаунт роблокс", 10)]
    res = await pricing.run(None, sessions, seller, PlayerokClient("T"), force=True)
    assert UPDATES == [{"id": "my1", "price": 94}], UPDATES  # (105 − 1) / 1.1 → 94
    assert res[0][2] and "100 Robux промокод" in res[0][1], res
    print("2. правило по названию лота:", res[0][1])

    # --- разбор «на сколько» ---
    pc, C = pricing.parse_cut, pricing.Cut
    assert pc("682") == C("set", 682) and pc("=682") == C("set", 682) and pc("до 682 ₽") == C("set", 682)
    assert pc("-10") == C("rub", 10) and pc("−10 ₽") == C("rub", 10) and pc("на 10") == C("rub", 10)
    assert pc("5%") == C("pct", 5) and pc("-5%") == C("pct", 5) and pc("12,5р") == C("set", 12.5)
    assert pc("0") is None and pc("100%") is None and pc("abc") is None
    # не дешевле половины за раз и не дороже текущей
    assert C("rub", 682).skip_reason(700) and C("set", 18).skip_reason(700) and C("set", 800).skip_reason(700)
    assert C("set", 682).skip_reason(700) is None and C("pct", 50).skip_reason(700) is None
    # --- выбор своих лотов ---
    items = await PlayerokClient("T").my_items("me", statuses=["APPROVED"])
    sel = lambda q: sorted(i.id for i in pricing.select_my_lots(items, q))
    assert sel("робукс") == ["my1", "my2"] and sel("100 робукс") == ["my1"]
    assert sel("ключ steam random") == ["my3"] and sel("https://playerok.com/products/steam?x=1") == ["my3"]
    assert sel("fortnite") == []
    print("3. формат суммы и выбор лотов")

    # --- ✂️ разово: робуксы на 10 % ---
    UPDATES.clear(); OUT.clear()
    MINE["my1"].update(price=110, rawPrice=100)
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await hp.cut_start(Cb("dp:cut"), state, sessions)
    await hp.cut_lots(Msg("fortnite"), state, sessions, cipher)
    assert "ничего не нашёл" in OUT[-1][0] and await state.get_state() == hp.CutPrice.lots.state
    await hp.cut_lots(Msg("робукс"), state, sessions, cipher)
    text, kb = OUT[-1]
    assert "Нашёл 2" in text and await state.get_state() == hp.CutPrice.pick.state, text
    assert buttons(kb) == ["dp:cs:0", "dp:cs:1", "dp:csp:330", "dp:csp:110", "dp:csall", "dp:csnone", "dp:csok"], buttons(kb)
    assert kb.inline_keyboard[0][0].text.startswith("✅ 330 ₽ · 400 робуксов")  # дорогие сверху
    # галочки: снять одну, «ни одного» → «Дальше» не пускает, «все» → дальше
    cb = Cb("dp:cs:0"); cb.message = Msg()
    await hp.cut_pick_toggle(cb, state)
    assert (await state.get_data())["cut_sel"] == [1]
    await hp.cut_pick_all(Cb("dp:csnone"), state)
    cb = Cb("dp:csok")
    await hp.cut_pick_ok(cb, state)
    assert cb.alerts and await state.get_state() == hp.CutPrice.pick.state
    await hp.cut_pick_all(Cb("dp:csall"), state)
    await hp.cut_pick_ok(Cb("dp:csok"), state)
    assert "Снижаю 2" in OUT[-1][0] and "330 ₽" in OUT[-1][0] and await state.get_state() == hp.CutPrice.amount.state, OUT[-1][0]
    await hp.cut_amount(Msg("сто"), state)
    assert "Не понял" in OUT[-1][0]
    await hp.cut_amount(Msg("-682"), state)  # как на скриншоте: снизить на 682 ₽ — опечатка, не предлагаем
    assert "Нечего снижать" in OUT[-1][0] and OUT[-1][1] is None and await state.get_state() == hp.CutPrice.amount.state
    await hp.cut_amount(Msg("10%"), state)
    text, kb = OUT[-1]
    assert "110 ₽ → <b>~99 ₽</b>" in text and "330 ₽ → <b>~297 ₽</b>" in text and buttons(kb) == ["dp:cutok", "dp:cutno"], text
    assert UPDATES == []  # до подтверждения ничего не меняем
    await hp.cut_apply(Cb("dp:cutok"), state, sessions, cipher)
    assert UPDATES == [{"id": "my2", "price": 270}, {"id": "my1", "price": 90}], UPDATES
    final = OUT[-1][0]
    assert "Снижено: 2 из 2" in final and "110 ₽ → 99 ₽" in final and "330 ₽ → 297 ₽" in final, final
    cb = Cb("dp:cutok")  # повторное нажатие — ничего не делает
    await hp.cut_apply(cb, state, sessions, cipher)
    assert len(UPDATES) == 2 and cb.alerts and "Устарело" in cb.alerts[0]
    print("4. разово на 10 %:", final.replace("\n", " | "))

    # --- поставить цену; дороже текущей — не трогаем; цена из ответа Playerok ---
    PRICE_IN_ANSWER["on"] = True
    note, ok = await pricing.cut_price(PlayerokClient("T"), "my3", pricing.Cut("set", 44))
    assert ok and UPDATES[-1] == {"id": "my3", "price": 40} and note == "55 ₽ → 44 ₽", note
    n = len(UPDATES)
    note, ok = await pricing.cut_price(PlayerokClient("T"), "my3", pricing.Cut("set", 60))
    assert not ok and len(UPDATES) == n and "не дороже" in note, note
    # отмена
    await hp.cut_start(Cb("dp:cut"), state, sessions)
    await hp.cut_lots(Msg("ключ"), state, sessions, cipher)
    await hp.cut_amount(Msg("5"), state)
    await hp.cut_cancel(Cb("dp:cutno"), state, sessions)
    assert await state.get_state() is None and len(UPDATES) == n
    print("5. «=цена», не поднимает, отмена")

    # --- скриншот: два лота с одним названием, 700 и 684 ₽; снижаем только те, что за 684 ---
    MINE["my1"].update(price=700, rawPrice=636); MINE["my2"].update(price=684, rawPrice=622)
    await hp.cut_start(Cb("dp:cut"), state, sessions)
    await hp.cut_lots(Msg("робукс"), state, sessions, cipher)
    assert "dp:csp:684" in buttons(OUT[-1][1]) and "dp:csp:700" in buttons(OUT[-1][1])
    await hp.cut_pick_price(Cb("dp:csp:684"), state)
    await hp.cut_pick_ok(Cb("dp:csok"), state)
    assert "Снижаю 1" in OUT[-1][0] and "700" not in OUT[-1][0], OUT[-1][0]
    await hp.cut_amount(Msg("682"), state)
    text, kb = OUT[-1]
    assert "Поставить цену 682 ₽" in text and "684 ₽ → <b>~682 ₽</b>" in text and "700 ₽" not in text, text
    assert kb.inline_keyboard[0][0].text == "✅ Да, снизить (1)"
    n = len(UPDATES)
    await hp.cut_apply(Cb("dp:cutok"), state, sessions, cipher)
    final = OUT[-1][0]
    assert "Снижено: 1 из 1" in final and "684 ₽ → 682" in final, final
    assert UPDATES[n:] == [{"id": "my2", "price": 620}], UPDATES[n:]  # лот за 700 не трогали
    # ошибка Playerok о минимальной цене — по-русски
    MIN_RAW["v"] = 620
    note, ok = await pricing.cut_price(PlayerokClient("T"), "my1", pricing.Cut("set", 682))
    assert not ok and "Playerok не даёт цену ниже 620 ₽" in note, note
    print("6. только лоты за 684 ₽:", final.replace("\n", " | "))

    # --- скриншот 2: «Minimal discount -» без числа — по-русски и с подробностями из extensions ---
    MIN_DISCOUNT["on"], MIN_RAW["v"] = True, 0
    MINE["my2"].update(price=684, rawPrice=622)
    n = len(UPDATES)
    note, ok = await pricing.cut_price(PlayerokClient("T"), "my2", pricing.Cut("set", 682))
    assert not ok and len(UPDATES) == n, note
    assert "не даёт снизить цену так мало" in note and '"minDiscount": 5' in note, note
    note, ok = await pricing.cut_price(PlayerokClient("T"), "my2", pricing.Cut("pct", 10))
    assert ok and note.startswith("684 ₽ → 61"), note  # сильнее — проходит
    print("7. «Minimal discount»:", note)
    print("OK")
asyncio.run(main())
