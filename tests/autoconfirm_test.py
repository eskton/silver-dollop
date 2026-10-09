import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from datetime import timedelta
from sqlalchemy import select
from bot.db import init_db, DealState, DeliveryItem
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

M = {"fail": False, "updates": 0, "status": "PAID", "applied_with_500": False}
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
        if M["applied_with_500"]:  # как вживую: 500 «Something gone wrong», но статус поменялся
            M["status"] = "SENT"
            return RawResponse(200, json.dumps({"errors": [{"message": "Something gone wrong, please try again later",
                                                            "extensions": {"statusCode": 500}}]}), None)
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

async def run(fail, applied_with_500=False, polls=3):
    M.update(fail=fail, updates=0, status="PAID", applied_with_500=applied_with_500)
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
    for _ in range(polls):
        await sync_seller(bot, sessions, cipher, seller, notify=True)
        async with sessions() as s:  # прошла минута — можно повторять
            st = await s.scalar(select(DealState))
            if st and st.confirm_tried_at:
                st.confirm_tried_at -= timedelta(minutes=2)
                await s.commit()
    return bot

async def main():
    bot = await run(fail=False)
    assert M["updates"] == 1 and any("отмечен выполненным" in t for t in bot.sent), bot.sent
    print("1. автоподтверждение с первого раза")

    # Playerok ответил 500, но заказ отметил — со следующим опросом это видно, ошибки продавцу нет
    bot = await run(fail=False, applied_with_500=True)
    assert M["updates"] == 1, M["updates"]
    assert not [t for t in bot.sent if "Не смог отметить" in t], bot.sent
    assert sum("отмечен выполненным" in t for t in bot.sent) == 1, bot.sent
    print("2. ответ 500, но статус поменялся — засчитано, без ошибки")

    # постоянная ошибка: 3 попытки (не чаще раза в минуту), потом одно сообщение продавцу
    bot = await run(fail=True, polls=6)
    errs = [t for t in bot.sent if "Не смог отметить" in t]
    assert len(errs) == 1 and "Invalid status transition" in errs[0] and "3 попытки" in errs[0], bot.sent
    assert M["updates"] == 3, M["updates"]  # после трёх попыток не долбит Playerok
    print("3. три попытки, потом одно сообщение")

    # без паузы в минуту повтор не делается
    M.update(fail=True, updates=0, status="PAID")
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.token_enc = cipher.encrypt("T"); await s.commit()
        await ft.set_setting(s, 1, "autodelivery_enabled", "1")
        await ft.set_setting(s, 1, "autoconfirm_enabled", "1")
        s.add(DeliveryItem(seller_tg_id=1, item_name="Лот", item_key="лот", content="CODE1")); await s.commit()
    for _ in range(3):
        await sync_seller(Bot(), sessions, cipher, seller, notify=True)
    assert M["updates"] == 1, M["updates"]
    print("4. повтор не чаще раза в минуту")
    print("OK")
asyncio.run(main())
