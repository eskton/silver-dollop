import asyncio, json, os, sys
from datetime import datetime, timedelta
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from bot.crypto import TokenCipher
from bot.db import init_db, DeliveryItem, DealState
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.automation import match_stock_key
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

DEALS = []
CHAT = {}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "deals":
        edges = [{"node": {"id": d, "status": "PAID", "createdAt": ca, "item": {"id": "i", "name": name, "price": 5},
                           "user": {"id": "b", "username": "ToliiK123"}, **({"chat": {"id": c}} if c else {})}}
                 for d, name, c, ca in DEALS]
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": edges}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        CHAT.setdefault(v["input"]["chatId"], []).append(v["input"]["text"])
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"data": {"items": {"edges": []}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
OLD = (datetime.utcnow() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, 1, "autodelivery_enabled", "1")
        stock = "💰 50 РОБУКСОВ | ПРОМОКОД | ВЫДАЧА ЗА 1 СЕК 💎"
        s.add(DeliveryItem(seller_tg_id=1, item_name=stock, item_key=stock.lower(), content="CODE50"))
        s.add(DeliveryItem(seller_tg_id=1, item_name="150 робуксов", item_key="150 робуксов", content="CODE150"))
        await s.commit()
        # 150 не путается с 50
        assert await match_stock_key(s, 1, "50 робуксов промокод", only_free=True) == stock.lower()
        assert await match_stock_key(s, 1, "Ключ Steam", only_free=True) is None
    bot = Bot()

    # Название на Playerok отличается вариантом эмодзи и пробелами — всё равно выдаём
    DEALS.append(("d1", "💰 50 РОБУКСОВ |  ПРОМОКОД | ВЫДАЧА ЗА 1 СЕК 💎️", "c1", NOW))
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert any("CODE50" in m for m in CHAT.get("c1", [])), CHAT
    print("1. разное написание названия → код выдан")

    # Заказ без чата → выдачи нет, но продавцу предупреждение (один раз)
    async with sessions() as s:
        s.add(DeliveryItem(seller_tg_id=1, item_name="150 робуксов", item_key="150 робуксов", content="CODE150B"))
        await s.commit()
    DEALS.append(("d2", "150 робуксов", None, NOW))
    for _ in range(3):
        await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert sum("Не нашёл чат заказа" in t for t in bot.sent) == 1, bot.sent
    print("2. заказ без чата → одно предупреждение")

    # Запас добавили после оплаты → выдаётся на следующем опросе
    DEALS.append(("d3", "Аккаунт Fortnite", "c3", NOW))
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert not any("LOGIN:PASS" in m for m in CHAT.get("c3", []))
    async with sessions() as s:
        s.add(DeliveryItem(seller_tg_id=1, item_name="Аккаунт Fortnite", item_key="аккаунт fortnite", content="LOGIN:PASS"))
        await s.commit()
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert any("LOGIN:PASS" in m for m in CHAT.get("c3", [])), CHAT
    print("3. запас добавили позже → выдано на следующем опросе")

    # Старый (3 дня) неоплаченный-невыданный заказ — не трогаем
    async with sessions() as s:
        s.add(DeliveryItem(seller_tg_id=1, item_name="Старый лот", item_key="старый лот", content="OLDCODE"))
        await s.commit()
    DEALS.append(("d4", "Старый лот", "c4", OLD))
    async with sessions() as s:  # бот видел этот заказ раньше (например, при импорте истории)
        s.add(DealState(seller_tg_id=1, deal_id="d4", item_name="Старый лот", status="PAID",
                        created_at=datetime.utcnow() - timedelta(days=3)))
        await s.commit()
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert not any("OLDCODE" in m for m in CHAT.get("c4", [])), CHAT.get("c4")
    print("4. старый заказ (3 дня) → не выдаём повторно")
    print("OK")
asyncio.run(main())
