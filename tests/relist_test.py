import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from bot.crypto import TokenCipher
from bot.db import init_db, ActionLog, Seller
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok.client import PlayerokClient, RawResponse
from bot.playerok import AuthRequired, PlayerokError
from bot.services import features as ft
from bot.services.automation import process_items, relist_candidates
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller

MODE = {"items_fail": None, "published": []}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals": return RawResponse(200, json.dumps({"data":{"deals":{"pageInfo":{"hasNextPage":False},"edges":[]}}}), None)
    if op == "chats": return RawResponse(200, json.dumps({"data":{"chats":{"edges":[]}}}), None)
    if op == "items":
        if MODE["items_fail"] == "auth":
            return RawResponse(200, json.dumps({"errors":[{"message":"Unauthorized","extensions":{"code":"FORBIDDEN"}}]}), None)
        if MODE["items_fail"] == "err":
            return RawResponse(200, json.dumps({"errors":[{"message":"Something broke"}]}), None)
        return RawResponse(200, json.dumps({"data":{"items":{"edges":[
            {"node":{"id":"i1","slug":"a","name":"Лот А","status":"SOLD","price":10}},
            {"node":{"id":"i2","slug":"b","name":"Лот Б","status":"EXPIRED","price":20}},
            {"node":{"id":"i3","slug":"c","name":"Активный","status":"APPROVED","price":30}}]}}}), None)
    if op == "publishItem":
        MODE["published"].append(v["input"]["itemId"])
        return RawResponse(200, json.dumps({"data":{"publishItem":{"id":v["input"]["itemId"]}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    def __init__(self): self.sent=[]
    async def send_message(self,*a,**k): self.sent.append(a)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id="u1"; seller.token_enc=cipher.encrypt("T"); await s.commit()
        await ft.set_setting(s, 1, "relist_enabled", "1")
        await ft.set_setting(s, 1, "relist_interval_hours", "0")

    # кандидаты: только проданные/истёкшие
    async with sessions() as s:
        async with PlayerokClient("T") as c:
            items = await c.my_items("u1")
        cand = await relist_candidates(s, 1, items)
    assert [i.id for i in cand] == ["i1","i2"], [i.id for i in cand]

    # авто-восстановление по интервалу (0ч)
    bot = Bot()
    await process_items(bot, sessions, seller, PlayerokClient("T"))
    assert MODE["published"] == ["i1","i2"], MODE["published"]

    # items падает "auth" — продавец НЕ разлогинивается (sync_seller проходит без AuthRequired наружу)
    MODE["items_fail"]="auth"; MODE["published"].clear()
    await sync_seller(bot, sessions, cipher, seller, notify=True)  # не должно бросить
    async with sessions() as s:
        assert (await s.get(Seller,1)).token_enc is not None  # остался в системе

    # items падает обычной ошибкой — тоже не роняет
    MODE["items_fail"]="err"
    await process_items(bot, sessions, seller, PlayerokClient("T"))
    print("OK")
asyncio.run(main())
