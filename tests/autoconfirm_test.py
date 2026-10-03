import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from bot.db import init_db, DeliveryItem
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

M = {"fail": False, "updates": 0, "status": "PAID"}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "deals":
        node = {"id": "d1", "status": M["status"], "item": {"id": "i", "name": "Лот", "price": 5},
                "user": {"id": "b", "username": "buyer"}, "chat": {"id": "c1"}}
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": [{"node": node}]}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "updateDeal":
        M["updates"] += 1
        if M["fail"]:
            return RawResponse(200, json.dumps({"errors": [{"message": "Invalid status transition"}]}), None)
        M["status"] = "SENT"
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": "d1"}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"data": {"items": {"edges": []}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, **kw): self.sent.append(text)

async def run(fail):
    M.update(fail=fail, updates=0, status="PAID")
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, 1, "autodelivery_enabled", "1")
        await ft.set_setting(s, 1, "autoconfirm_enabled", "1")
        s.add(DeliveryItem(seller_tg_id=1, item_name="Лот", item_key="лот", content="CODE1"))
        await s.commit()
    bot = Bot()
    for _ in range(3):
        await sync_seller(bot, sessions, cipher, seller, notify=True)
    return bot

async def main():
    bot = await run(fail=False)
    assert M["updates"] == 1 and any("отмечен выполненным" in t for t in bot.sent), bot.sent
    bot = await run(fail=True)
    errs = [t for t in bot.sent if "Не смог отметить" in t]
    assert len(errs) == 1 and "Invalid status transition" in errs[0], bot.sent
    assert M["updates"] == 1, M["updates"]  # после ошибки не долбит Playerok каждый опрос
    print("OK")
asyncio.run(main())
