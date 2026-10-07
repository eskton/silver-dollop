"""Снижение цен: дешевле конкурентов на шаг, не ниже минимума, только вниз."""
import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from bot.db import init_db, PriceRule
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft, pricing
from bot.services.sellers import get_or_create_seller

# мой лот: для покупателя 110 ₽, мне 100 ₽ (комиссия ×1.1)
MINE = {"id": "my1", "slug": "my-100", "name": "💰 100 РОБУКСОВ | ПРОМОКОД", "price": 110, "rawPrice": 100,
        "status": "APPROVED", "category": {"id": "cat-robux"}, "user": {"id": "me"},
        "obtainingType": {"id": "ot-code", "name": "Промокод"}}
RIVALS = []
UPDATES = []
IGNORE = {"on": False}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "items":
        f = v["filter"]
        if f.get("userId") == "me":
            return RawResponse(200, json.dumps({"data": {"items": {"edges": [{"node": MINE}], "pageInfo": {}}}}), None)
        assert f == {"gameCategoryId": "cat-robux", "status": ["APPROVED"], "obtainingTypeId": "ot-code"}, f
        edges = [{"node": n} for n in RIVALS + [MINE]]  # сервер мог и не отфильтровать — проверяем сами
        return RawResponse(200, json.dumps({"data": {"items": {"edges": edges, "pageInfo": {}}}}), None)
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": MINE}}), None)
    if op == "updateItem":
        UPDATES.append(v["input"])
        if IGNORE["on"]:  # Playerok ответил «ок», но цену не поменял
            return RawResponse(200, json.dumps({"data": {"updateItem": {"id": "my1", "status": "PENDING_MODERATION"}}}), None)
        MINE["rawPrice"] = v["input"]["price"]; MINE["price"] = round(v["input"]["price"] * 1.1, 2)
        return RawResponse(200, json.dumps({"data": {"updateItem": {"id": "my1"}}}), None)
    raise AssertionError(op)

def rival(i, name, price, user="u2", way="ot-code"):
    return {"id": f"r{i}", "name": name, "price": price, "status": "APPROVED", "user": {"id": user},
            "obtainingType": {"id": way}}

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

async def main():
    assert pricing.kw_match("100 робукс", "100 Робуксов промокод") and not pricing.kw_match("100 робукс", "1000 робуксов")
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"
        s.add(PriceRule(seller_tg_id=1, lot_key="https://playerok.com/products/my-100".split("/products/")[1],
                        competitor_kw="100 робукс", step=1, min_price=95))
        await s.commit()
        await ft.set_setting(s, 1, "dumping_enabled", "1")
    client = PlayerokClient("T", transport=pk)
    bot = Bot()

    # 1. конкурент 105 ₽ (и чужой 1000 робуксов за 50 ₽ — не тот товар), свой дешёвый лот не считаем
    RIVALS[:] = [rival(1, "100 Робуксов промокод", 105), rival(2, "1000 робуксов", 50), rival(3, "100 робуксов", 90, user="me"),
                 rival(4, "100 РОБУКСОВ СРАЗУ НА БАЛАНС ПО НИКУ", 70, way="ot-nick")]  # другой способ получения
    res = await pricing.run(bot, sessions, seller, client)
    assert UPDATES == [{"id": "my1", "price": 94}], UPDATES          # цель 104 ₽ / 1.1 = 94.5 → 94
    assert res[0][2] and "снизил" in res[0][1] and any("📉" in t for t in bot.sent)
    assert "playerok.com/products/" in res[0][1] and "100 Робуксов промокод" in res[0][1]  # видно, с кем сравнили
    print("1. дешевле конкурента на шаг:", res[0][1])

    # 2. интервал: повторно сразу не проверяет
    assert await pricing.run(bot, sessions, seller, client) == []
    # 3. уже дешевле всех — не трогаем (и цену не поднимаем)
    res = await pricing.run(bot, sessions, seller, client, force=True)
    assert len(UPDATES) == 1 and not res[0][2] and "дешевле всех" in res[0][1], res
    print("2-3. интервал соблюдается; уже дешевле — цену не трогает:", res[0][1])

    # 4. конкурент ниже минимума → ставим минимум, не ниже
    MINE.update(price=110, rawPrice=100)
    RIVALS[:] = [rival(1, "100 Робуксов", 60)]
    res = await pricing.run(bot, sessions, seller, client, force=True)
    assert UPDATES[-1]["price"] == 87 and MINE["price"] >= 95, (UPDATES, MINE)  # ceil(95/1.1)=87 → 95.7 ₽
    res = await pricing.run(bot, sessions, seller, client, force=True)
    assert len(UPDATES) == 2 and "минимум" in res[0][1], res
    print("4. не ниже минимума:", res[0][1])

    # 4b. Playerok ответил «ок», но цена не изменилась — не врём, что снизили
    MINE.update(price=158, rawPrice=144)
    RIVALS[:] = [rival(9, "100 Робуксов", 140)]
    IGNORE["on"] = True
    res = await pricing.run(bot, sessions, seller, client, force=True)
    IGNORE["on"] = False
    assert not res[0][2] and "цена не изменилась" in res[0][1] and "PENDING_MODERATION" in res[0][1], res[0][1]
    print("4b. цена не поменялась — честное сообщение:", res[0][1][:120])

    # 5. нет конкурентов / лот не найден — понятные пометки
    RIVALS[:] = []
    res = await pricing.run(bot, sessions, seller, client, force=True)
    assert "конкурентов" in res[0][1]
    async with sessions() as s:
        r = await s.scalar(select(PriceRule)); r.lot_key = "несуществующий"; await s.commit()
    res = await pricing.run(bot, sessions, seller, client, force=True)
    assert "не найден" in res[0][1]
    async with sessions() as s:
        assert (await s.scalar(select(PriceRule))).last_note.startswith("мой лот не найден")
    print("5. пометки: нет конкурентов / лот не найден")

    # 6. экран
    from bot.handlers.pricing import render_dumping
    text, kb = await render_dumping(sessions, seller, ft.FEATURE_BY_KEY["dumping"])
    btns = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert "dp:add" in btns and "dp:run" in btns and "f:dumping:t" in btns and "не найден" in text
    print("OK")
asyncio.run(main())

# ----- цены по номиналам -----
async def nominal():
    lots = [
        {"id": "a", "slug": "a", "name": "✅ДЛЯ РФ✅100 РОБУКСОВ ПО КОДУ", "price": 120, "obtainingType": {"id": "code"}},
        {"id": "b", "slug": "b", "name": "100 робуксов промокод", "price": 104, "rawPrice": 94, "obtainingType": {"id": "code"}},
        {"id": "c", "slug": "c", "name": "500 робуксов", "price": 520, "obtainingType": {"id": "code"}},
        {"id": "d", "slug": "d", "name": "100 робуксов по нику", "price": 50, "obtainingType": {"id": "nick"}},
        {"id": "e", "slug": "e", "name": "Робуксы любые", "price": 10, "obtainingType": {"id": "code"}},
    ]
    async def t(body, token):
        op, v = body["operationName"], body["variables"]
        if op == "item":
            assert v["slug"] == "my-lot"
            return RawResponse(200, json.dumps({"data": {"item": {"id": "m", "name": "x", "category": {"id": "cat"},
                               "price": 110, "rawPrice": 100,
                               "obtainingType": {"id": "code", "name": "Промокод"}}}}), None)
        if op == "items":
            assert v["filter"]["obtainingTypeId"] == "code"
            return RawResponse(200, json.dumps({"data": {"items": {"edges": [{"node": n} for n in lots], "pageInfo": {}}}}), None)
        raise AssertionError(op)
    costs = pricing.parse_costs("100 0.5\n500=5,5$")
    assert costs == {100: 0.5, 500: 5.5}
    text = await pricing.nominal_report(PlayerokClient("T", transport=t), "https://playerok.com/products/my-lot", 104, costs=costs)
    print(text)
    # 100: продавец получает rawPrice 94 ₽ → $0.90, закупка 0.5 → +$0.40 ✅
    assert "<b>100</b> · мин. 104 ₽ (продавцу 94 ₽) → <b>$0.90</b>" in text and "прибыль <b>$+0.40</b> (+81%) ✅" in text, text
    # 500: rawPrice нет → по доле своего лота 100/110: 520×0.909=472.7 ₽ → $4.55, закупка 5.5 → минус ❌
    assert "(продавцу ≈472.73 ₽) → <b>$4.55</b>" in text and "прибыль <b>$-0.95</b>" in text and "❌" in text, text
    assert "по нику" not in text and "любые" not in text
    print("OK")
asyncio.run(nominal())
