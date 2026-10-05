import asyncio, json, logging, os, sys, tempfile
from datetime import datetime
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
logging.basicConfig(level=logging.INFO, stream=open(os.devnull, "w"))
from bot import logs as L
from bot.crypto import TokenCipher
from bot.db import init_db
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller
from bot.handlers.logs import send_logs

NOW = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
M = {"deals": [], "pub_fail": False}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals":
        edges = [{"node": {"id": d, "status": "PAID", "createdAt": NOW, "item": {"id": i, "name": n, "price": 5},
                           "user": {"id": "b", "username": "buyer"}, "chat": {"id": "c" + d}}} for d, i, n in M["deals"]]
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": edges}}}), None)
    if op == "chats":
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": []}}}), None)
    if op == "createChatMessage":
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "publishItem":
        if M["pub_fail"]:
            return RawResponse(200, json.dumps({"errors": [{"message": "Item is not sold"}]}), None)
        return RawResponse(200, json.dumps({"data": {"publishItem": {"id": "x"}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"errors": [{"message": "Access denied"}]}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

class Bot:
    async def send_message(self, *a, **k): pass

class Msg:
    def __init__(self, uid): self.from_user = SimpleNamespace(id=uid); self.docs = []; self.texts = []
    async def answer(self, t, **k): self.texts.append(t)
    async def answer_document(self, doc, caption=None): self.docs.append((doc.data.decode(), caption))

async def main():
    d = tempfile.mkdtemp()
    L.setup_file_logging(d)
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    sellers = []
    for uid in (1, 2):
        async with sessions() as s:
            sl = await get_or_create_seller(s, SimpleNamespace(id=uid, username=f"u{uid}"))
            sl.playerok_id = f"me{uid}"; sl.playerok_username = f"acc{uid}"; sl.token_enc = cipher.encrypt("T")
            await s.commit()
            sellers.append(sl)

    # 1) функция выключена → причина в логе (один раз)
    M["deals"] = [("d1", "item-1", "50 робуксов")]
    for _ in range(3):
        await sync_seller(Bot(), sessions, cipher, sellers[0], notify=True)
    text = L.read_tail()
    assert text.count("сделка d1 «50 робуксов» пропущена — функция «Автовыставление лотов» выключена") == 1, text

    # 2) включена → выставлено; ошибка Playerok → текст ошибки в логе
    async with sessions() as s:
        await ft.set_setting(s, 1, "relist_enabled", "1")
        await ft.set_setting(s, 2, "relist_enabled", "1")
    M["deals"] = [("d2", "item-2", "100 робуксов")]
    await sync_seller(Bot(), sessions, cipher, sellers[0], notify=True)
    M["pub_fail"] = True
    M["deals"] = [("d3", "item-3", "Ключ")]
    await sync_seller(Bot(), sessions, cipher, sellers[1], notify=True)
    text = L.read_tail()
    assert "[tg=1] перевыставление: лот item-2 выставлен заново (сделка d2)" in text, text
    assert "[tg=2] перевыставление: Playerok отказал для лота item-3 (сделка d3): publishItem: Item is not sold" in text, text

    # 3) /logs: владелец получает всё, продавец 2 — только свои строки
    owner = Msg(7534591041); await send_logs(owner)
    body, cap = owner.docs[0]
    assert "[tg=1]" in body and "[tg=2]" in body and "все записи" in cap
    seller2 = Msg(2); await send_logs(seller2)
    body2, _ = seller2.docs[0]
    assert "[tg=2]" in body2 and "[tg=1]" not in body2, body2
    print("OK")
asyncio.run(main())
