import asyncio, json, os, sys
from datetime import datetime
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from bot.db import init_db, DealState
from bot.playerok.client import PlayerokClient, RawResponse
from bot.playerok import PlayerokError
from bot.services.history import import_history, import_history_quietly
from bot.services.analytics import build_report
from bot.services.sellers import get_or_create_seller
from sqlalchemy import select, func

PAGES = {None: ("c1", range(1, 51)), "c1": ("c2", range(51, 101)), "c2": (None, range(101, 123))}
calls = []

def make_transport(fail_on=None):
    async def t(body, token):
        op, v = body["operationName"], body["variables"]
        assert op == "deals", op
        after = v["pagination"].get("after")
        calls.append(after)
        if fail_on is not None and after == fail_on:
            return RawResponse(200, json.dumps({"errors": [{"message": "Cannot query field pageInfo"}]}), None)
        nxt, ids = PAGES[after]
        edges = [{"node": {"id": f"d{i}", "status": "COMPLETED", "createdAt": "2026-09-01T00:00:00Z",
                           "item": {"name": f"Лот {i % 3}", "price": 10}, "user": {"username": f"b{i % 7}"}}} for i in ids]
        if after == "c1":  # дубль с предыдущей страницы
            edges.append({"node": {"id": "d50", "status": "COMPLETED", "item": {"name": "x"}, "user": {}}})
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": nxt is not None, "endCursor": nxt}, "edges": edges}}}), None)
    return t

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None): self.sent.append(text)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "u1"; seller.token_enc = cipher.encrypt("T"); await s.commit()

    client = PlayerokClient("T", transport=make_transport())
    n = await import_history(Bot(), sessions, cipher, seller, client=client)
    assert n == 122, n
    assert calls == [None, "c1", "c2"], calls
    async with sessions() as s:
        assert await s.scalar(select(func.count(DealState.id))) == 122
        r = await build_report(s, 1, now=datetime(2026, 10, 2))
    assert "<b>Всё время:</b> 122 зак. · 1 220 ₽" in r, r

    # повторный импорт ничего не дублирует
    n = await import_history(Bot(), sessions, cipher, seller, client=PlayerokClient("T", transport=make_transport()))
    async with sessions() as s:
        assert await s.scalar(select(func.count(DealState.id))) == 122

    # ошибка GraphQL доходит до продавца текстом
    bot = Bot()
    PlayerokClient._curl_post = lambda self, body, token: make_transport(fail_on=None)(body, token)
    try:
        await import_history(bot, sessions, cipher, seller, client=PlayerokClient("T", transport=make_transport(fail_on="c1")))
        raise AssertionError("ожидали ошибку")
    except PlayerokError as e:
        assert "pageInfo" in str(e)
    await import_history_quietly(bot, sessions, cipher, seller)  # транспорт без ошибки → успех
    assert bot.sent[-1].startswith("📊 Загрузил историю заказов: 122"), bot.sent
    print("OK")

asyncio.run(main())
