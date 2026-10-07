"""«🧹 Снять лоты, выставленные ботом за 24 ч»: снимает только выставленные ботом и ещё активные."""
import asyncio, json, os, sys
from datetime import datetime, timedelta
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from bot.crypto import TokenCipher
from bot.db import init_db, ActionLog
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services.sellers import get_or_create_seller
from bot.handlers.settings import relist_undo, relist_undo_ask

ITEMS = {"a": "APPROVED", "b": "APPROVED", "c": "SOLD"}  # c уже снова продан — не трогаем
DISC = []
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": {"id": v["id"], "name": "Лот " + v["id"], "status": ITEMS[v["id"]]}}}), None)
    if op.startswith("rest:"):
        DISC.append(body["rest"])
        return RawResponse(200, "", None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

OUT = []
class Msg:
    async def edit_text(self, text, reply_markup=None): OUT.append(text)
    async def answer(self, text, reply_markup=None, **k): OUT.append(text)
class Cb:
    def __init__(self, data): self.data = data; self.from_user = SimpleNamespace(id=1, username="u", first_name="u"); self.message = Msg()
    async def answer(self, *a, **k): pass

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "me"; seller.token_enc = cipher.encrypt("T")
        s.add_all([ActionLog(seller_tg_id=1, kind="relist", target=t) for t in ("a", "b", "c", "a")])
        s.add(ActionLog(seller_tg_id=1, kind="relist", target="old", created_at=datetime.utcnow() - timedelta(days=3)))
        await s.commit()
    await relist_undo_ask(Cb("rl:undo"), sessions)
    assert "3 шт" in OUT[-1], OUT[-1]
    await relist_undo(Cb("rl:undook"), sessions, cipher)
    assert DISC == ["/rest-api/public/item/a/discontinue", "/rest-api/public/item/b/discontinue"], DISC
    assert "Снято с продажи: 2" in OUT[-1]
    async with sessions() as s:  # повторно уже нечего снимать, старые записи не тронуты
        assert await s.scalar(select(func.count(ActionLog.id)).where(ActionLog.target.in_(["a", "b", "c"]))) == 0
        assert await s.scalar(select(func.count(ActionLog.id)).where(ActionLog.target == "old")) == 1
    print("OK")
asyncio.run(main())
