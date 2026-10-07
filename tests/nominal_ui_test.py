"""«🌐 Выгода по рынку»: бот всегда отвечает — результат, частичный результат или ошибка."""
import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.crypto import TokenCipher
from bot.db import init_db
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services import features as ft
from bot.services.sellers import get_or_create_seller
from bot.handlers import pricing as hp, stats as hs

M = {"mode": "ok", "pages": 0}
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "item":
        return RawResponse(200, json.dumps({"data": {"item": {"id": "m", "name": "x", "category": {"id": "cat"},
                           "obtainingType": {"id": "code", "name": "Промокод"}}}}), None)
    if op == "items":
        M["pages"] += 1
        if M["mode"] == "crash":
            raise RuntimeError("boom")
        if M["mode"] == "limit" and M["pages"] >= 2:
            return RawResponse(200, json.dumps({"errors": [{"message": "Too many requests, please try again later"}]}), None)
        n = M["pages"]
        edges = [{"node": {"id": f"p{n}", "slug": f"p{n}", "name": f"{100 * n} робуксов", "price": 100 * n + 4,
                           "obtainingType": {"id": "code"}, "user": {"id": "other"}}}]
        return RawResponse(200, json.dumps({"data": {"items": {"edges": edges, "pageInfo": {"hasNextPage": True, "endCursor": str(n)}}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

SENT = []
class Status:
    def __init__(self, text): self.text = text
    async def edit_text(self, text, **k): SENT.append(("edit", text))
    async def delete(self): SENT.append(("delete", ""))
class Msg:
    async def answer(self, text, reply_markup=None, **k):
        SENT.append(("msg", text)); return Status(text)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    user = SimpleNamespace(id=1, username="u", first_name="u")
    async with sessions() as s:
        seller = await get_or_create_seller(s, user)
        seller.playerok_id = "me"; seller.token_enc = cipher.encrypt("T"); await s.commit()
        await ft.set_setting(s, 1, hp.NOMINAL_LINK_KEY, "https://playerok.com/products/my-lot")

    await hp._nominal(Msg(), sessions, cipher, user)
    final = SENT[-1][1]
    assert "<b>100</b> · 104 ₽" in final and "<b>500</b> · 504 ₽" in final and "частично" not in final, final
    assert any(k == "edit" and "страница" in t for k, t in SENT), SENT
    print("1. полный отчёт за 5 страниц, прогресс обновляется")

    SENT.clear(); M.update(mode="limit", pages=0)
    await hp._nominal(Msg(), sessions, cipher, user)
    final = SENT[-1][1]
    assert "<b>100</b>" in final and "частично" in final, final
    print("2. Playerok ограничил на 2-й странице — показан частичный результат")

    SENT.clear(); M.update(mode="crash", pages=0)
    await hp._nominal(Msg(), sessions, cipher, user)
    assert SENT[-1][1].startswith("⚠️"), SENT[-1]
    print("3. сбой — бот отвечает ошибкой, а не молчит")

    # кнопка в калькуляторе прибыли
    _, kb = await hs._profit_screen(sessions, 1)
    assert "dp:nom" in [b.callback_data for r in kb.inline_keyboard for b in r]
    print("OK")
asyncio.run(main())
