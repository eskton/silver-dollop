import asyncio, json, os, sys
from datetime import datetime
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from bot.db import init_db, DeliveryItem
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
M = {}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals":
        node = {"id": "d1", "status": M["status"], "createdAt": NOW, "item": {"id": "i1", "name": "50 робуксов", "price": 5},
                "user": {"id": "b", "username": "buyer"}, "chat": {"id": "c1"}}
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": [{"node": node}]}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        M["chat"].append(v["input"]["text"])
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "updateDeal":
        M["confirmed"].append(v["input"])
        M["status"] = "SENT"
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": "d1"}}}), None)
    if op in ("items", "publishItem"):
        return RawResponse(200, json.dumps({"data": {op: {"edges": []}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    async def send_message(self, *a, **k): pass

async def run(flag):
    M.update(status="PAID", chat=[], confirmed=[])
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        await ft.set_setting(s, 1, "autodelivery_enabled", "1")  # «Автоподтверждение» не включаем
        if flag is not None:
            await ft.set_setting(s, 1, "autodelivery_confirm", flag)
        s.add(DeliveryItem(seller_tg_id=1, item_name="50 робуксов", item_key="50 робуксов", content="CODE-1"))
        await s.commit()
    for _ in range(3):
        await sync_seller(Bot(), sessions, cipher, seller, notify=True)

async def main():
    await run(None)  # по умолчанию включено
    code_i = next(i for i, m in enumerate(M["chat"]) if "CODE-1" in m)
    ask_i = next(i for i, m in enumerate(M["chat"]) if "подтвердите получение" in m)
    assert M["confirmed"] == [{"id": "d1", "status": "SENT"}], M["confirmed"]
    assert code_i < ask_i, M["chat"]  # сначала код, потом просьба подтвердить
    print("1. код выдан → заказ отмечен выполненным → покупателя попросили подтвердить")
    await run("0")
    assert M["confirmed"] == [] and any("CODE-1" in m for m in M["chat"]), (M["confirmed"], M["chat"])
    print("2. переключатель выключен → код выдан, заказ не трогаем")
    print("OK")
asyncio.run(main())
