import asyncio, json, os, sys, tempfile
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from bot.crypto import TokenCipher
from bot.db import init_db, DeliveryItem, Seller
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

LOT = "💰 100 РОБУКСОВ | ПРОМОКОД | ВЫДАЧА ЗА 1 СЕК 💎"
DEALS = []          # (id, status, buyer, chat)
CHAT = {}           # chat_id -> [сообщения]
CONFIRMED = []
NOW = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "deals":
        edges = [{"node": {"id": d, "status": st, "createdAt": NOW,
                           "item": {"id": "i1", "slug": "robux", "name": LOT, "price": 90},
                           "user": {"id": b, "username": b}, "chat": {"id": c}}} for d, st, b, c in DEALS]
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": edges}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        CHAT.setdefault(v["input"]["chatId"], []).append(v["input"]["text"])
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "updateDeal":
        CONFIRMED.append(v["input"]["id"])
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": v["input"]["id"]}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"data": {"items": {"edges": []}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

async def counts(sessions):
    async with sessions() as s:
        free = await s.scalar(select(func.count(DeliveryItem.id)).where(DeliveryItem.used_deal_id.is_(None)))
        used = await s.scalar(select(func.count(DeliveryItem.id)).where(DeliveryItem.used_deal_id.is_not(None)))
    return free, used

async def main():
    db = os.path.join(tempfile.mkdtemp(), "bot.db")
    url = f"sqlite+aiosqlite:///{db}"
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    sessions = await init_db(url)
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, 1, "autodelivery_enabled", "1")
        await ft.set_template(s, 1, "delivery_msg", "Спасибо за покупку! Ваш промокод:")
        # название лота в запасе — в другом регистре, бот должен сопоставить
        for code in ("E4Y8Y-PXC7B-3ZQ44", "NFGR7-BE3PH-YKN4E", "NEYZ5-9F9AZ-ZT36W"):
            s.add(DeliveryItem(seller_tg_id=1, item_name=LOT, item_key=LOT.lower(), content=code))
        await s.commit()
    bot = Bot()

    # 1. Первый заказ → выдан первый код
    DEALS.append(("d1", "PAID", "Malisonlif", "c1"))
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert "Спасибо за покупку! Ваш промокод:\nE4Y8Y-PXC7B-3ZQ44" in CHAT["c1"], CHAT["c1"]
    assert await counts(sessions) == (2, 1)
    print("1. заказ d1 → выдан E4Y8Y, осталось 2")

    # 2. Тот же заказ при следующем опросе — повторно НЕ выдаётся
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert sum("Ваш промокод" in m for m in CHAT["c1"]) == 1
    print("2. повторный опрос → второй раз не выдано")

    # 3. «Перезапуск бота»: новое подключение к тому же файлу базы
    sessions = await init_db(url)
    async with sessions() as s:
        seller = await s.get(Seller, 1)
    assert await counts(sessions) == (2, 1)
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert sum("Ваш промокод" in m for m in CHAT["c1"]) == 1
    print("3. после перезапуска → выданный код остался выданным, d1 не выдан повторно")

    # 4. Два новых заказа → по коду каждому, разные коды
    DEALS.extend([("d2", "PAID", "Buyer2", "c2"), ("d3", "PAID", "Buyer3", "c3")])
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    code = lambda c: next(m for m in CHAT[c] if "Ваш промокод" in m).split("\n")[-1]
    got = {code("c2"), code("c3")}
    assert got == {"NFGR7-BE3PH-YKN4E", "NEYZ5-9F9AZ-ZT36W"}, got
    assert await counts(sessions) == (0, 3)
    assert any("Запас закончился" in t or "закончился" in t for t in bot.sent), bot.sent
    print("4. заказы d2, d3 → выданы разные коды, запас 0, пришло предупреждение")

    # 5. Запас пуст → четвёртый заказ не получает чужой/старый код
    DEALS.append(("d4", "PAID", "Buyer4", "c4"))
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert not any("Ваш промокод" in m for m in CHAT.get("c4", []))
    assert any("Заказ без товара" in t and "Buyer4" in t for t in bot.sent), bot.sent[-2:]
    print("5. запас пуст → d4 ничего не получил, повторной выдачи нет")
    print("OK")

asyncio.run(main())
