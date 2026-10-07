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
        "status": "APPROVED", "category": {"id": "cat-robux"}, "user": {"id": "me"}}
RIVALS = []
UPDATES = []
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "items":
        f = v["filter"]
        if f.get("userId") == "me":
            return RawResponse(200, json.dumps({"data": {"items": {"edges": [{"node": MINE}], "pageInfo": {}}}}), None)
        assert f == {"gameCategoryId": "cat-robux", "status": ["APPROVED"]}, f
        edges = [{"node": n} for n in RIVALS + [MINE]]
        return RawResponse(200, json.dumps({"data": {"items": {"edges": edges, "pageInfo": {}}}}), None)
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": MINE}}), None)
    if op == "updateItem":
        UPDATES.append(v["input"])
        MINE["rawPrice"] = v["input"]["price"]; MINE["price"] = round(v["input"]["price"] * 1.1, 2)
        return RawResponse(200, json.dumps({"data": {"updateItem": {"id": "my1"}}}), None)
    raise AssertionError(op)

def rival(i, name, price, user="u2"):
    return {"id": f"r{i}", "name": name, "price": price, "status": "APPROVED", "user": {"id": user}}

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
    RIVALS[:] = [rival(1, "100 Робуксов промокод", 105), rival(2, "1000 робуксов", 50), rival(3, "100 робуксов", 90, user="me")]
    res = await pricing.run(bot, sessions, seller, client)
    assert UPDATES == [{"id": "my1", "price": 94}], UPDATES          # цель 104 ₽ / 1.1 = 94.5 → 94
    assert res[0][2] and "снизил" in res[0][1] and any("📉" in t for t in bot.sent)
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
