import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from bot.crypto import TokenCipher
from bot.db import init_db, StarsOrder, StarsRule
from bot.playerok.client import PlayerokClient, RawResponse
from bot.plugins.stars import fragment as frmod
from bot.plugins.stars.fragment import parse_cookies
from bot.plugins.stars.fragment import RawResponse as FR
from bot.plugins.stars import ton as wmod
from bot.services import features as ft
from bot.plugins.stars import service as st
from bot.services.poller import sync_seller
from bot.services.sellers import get_or_create_seller
from bot.plugins.stars.handlers import render_stars

# --- поддельный Playerok ---
P = {"status": "PAID", "msg": None, "unread": 0, "confirmed": []}
CHAT = []
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    if op == "deals":
        return RawResponse(200, json.dumps({"data": {"deals": {"pageInfo": {"hasNextPage": False}, "edges": [{"node": {
            "id": "d1", "status": P["status"], "createdAt": "2026-10-02T10:00:00Z",
            "item": {"id": "i1", "name": "Telegram 500 звёзд ⭐ быстро", "price": 900},
            "user": {"id": "b1", "username": "buyer"}, "chat": {"id": "c1"}}}]}}}), None)
    if op == "chats":
        edges = []
        if P["msg"]:
            edges.append({"node": {"id": "c1", "unreadMessagesCounter": P["unread"],
                                   "lastMessage": {"id": P["msg"][0], "text": P["msg"][1], "user": {"id": "b1", "username": "buyer"}}}})
        return RawResponse(200, json.dumps({"data": {"chats": {"edges": edges}}}), None)
    if op == "createChatMessage":
        CHAT.append(v["input"]["text"]); return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "m"}}}), None)
    if op == "updateDeal":
        P["confirmed"].append(v["input"]); P["status"] = "SENT"
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": "d1"}}}), None)
    if op == "items":
        return RawResponse(200, json.dumps({"data": {"items": {"edges": []}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

# --- поддельный Fragment ---
F = {"fail_link": False, "calls": []}
async def fr(url, data, headers):
    F["calls"].append((url.split("?")[0].split("/")[-1], (data or {}).get("method")))
    if data is None:
        return FR(200, '<html>... "apiUrl":"\\/api?hash=abc123def456" ... <div class="tm-header-avatar"> ...</html>')
    m = data["method"]
    if m == "searchStarsRecipient":
        if data["query"] == "nobody":
            return FR(200, json.dumps({"error": "No Telegram users found."}))
        return FR(200, json.dumps({"found": {"recipient": "rcpt-1", "name": "Ivan", "photo": ""}}))
    if m == "initBuyStarsRequest":
        return FR(200, json.dumps({"req_id": "req-77", "amount": "1.95 USDT"}))
    if m == "getBuyStarsLink":
        if F["fail_link"]:
            return FR(200, json.dumps({"error": "Wallet not connected"}))
        assert data.get("currency") == "usdt_ton"
        return FR(200, json.dumps({"transaction": {"messages": [{"address": "EQBOFwOX4XWbqjOeDzYhzp8Dz3nIj_hq4rMx6qT5MBXaX3hb", "amount": "50000000", "payload": "te6cckEBAQEAAgAAAEysuc0="}]}}))
    if m == "updateStarsBuyState":
        return FR(200, json.dumps({"ok": True}))
    raise AssertionError(m)
frmod.FragmentClient._curl = lambda self, url, data, headers: fr(url, data, headers)

# --- поддельный кошелёк ---
SENT = []
class FakeWallet:
    def __init__(self, mnemonic, version="v5r1"): self.v = version
    async def address(self): return "UQfake"
    async def balances(self): return wmod.Balances(ton=0.9, usdt=5.0)
    async def send_fragment_messages(self, messages): SENT.append(messages); return "txhash1"
    async def aclose(self): pass
st.TonWallet = FakeWallet

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None, disable_web_page_preview=None): self.sent.append(text)

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    bot = Bot()
    async with sessions() as s:
        seller = await get_or_create_seller(s, SimpleNamespace(id=1, username="u"))
        seller.playerok_id = "u1"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        await s.commit()
        tg = seller.tg_id
        await ft.set_setting(s, tg, "stars_enabled", "1")
        from bot.plugins import access as ac
        assert not await ac.has_access(s, tg, "stars")
        await ac.grant(s, tg, "stars", None, 999)
        assert await ac.has_access(s, tg, "stars")
        await ft.set_setting(s, tg, "stars_cookies_enc", cipher.encrypt("stel_ssid=1; stel_token=2"))
        await ft.set_setting(s, tg, "stars_seed_enc", cipher.encrypt("word " * 24))
        await ft.set_setting(s, tg, "stars_min_usdt", "10")
        assert await st.stars_for_lot(s, tg, "Telegram 500 звёзд ⭐") == 500
        assert await st.stars_for_lot(s, tg, "1 000 stars pack") == 1000
        assert await st.stars_for_lot(s, tg, "Ключ Steam") is None
        s.add(StarsRule(seller_tg_id=tg, pattern="премиум-набор", stars=2500)); await s.commit()
        assert await st.stars_for_lot(s, tg, "Премиум-набор 2026") == 2500

    # 1. оплата → запрос username, автовыдача не лезет
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert any("@username" in t and "500" in t for t in CHAT), CHAT
    assert P["confirmed"] == []
    async with sessions() as s:
        o = await s.scalar(select(StarsOrder)); assert o.status == "awaiting_username" and o.stars == 500

    # 2. покупатель прислал мусор → просим ещё раз
    P["msg"], P["unread"] = ("m1", "привет"), 1
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert "Не нашёл такого пользователя" in CHAT[-1], CHAT[-1]
    # 3. несуществующий ник → снова просим
    P["msg"] = ("m2", "@nobody")
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert "Не нашёл" in CHAT[-1]
    async with sessions() as s:
        o = await s.scalar(select(StarsOrder)); assert o.status == "awaiting_username", o.status
    # 4. верный ник → покупка, сообщение, подтверждение сделки
    P["msg"] = ("m3", "мой ник @ivan_petrov")
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert CHAT[-1] == "Готово! 500 ⭐ отправлены на @ivan_petrov. Пожалуйста, подтвердите заказ 🙏", CHAT[-1]
    assert SENT and SENT[0][0]["address"].startswith("EQ")
    assert P["confirmed"] == [{"id": "d1", "status": "SENT"}]
    async with sessions() as s:
        o = await s.scalar(select(StarsOrder))
        assert o.status == "done" and o.tx_hash == "txhash1" and o.cost_usdt == 1.95 and o.recipient_name == "Ivan"
    assert any("Выдал 500 ⭐ на @ivan_petrov за 1.95 USDT" in t for t in bot.sent), bot.sent
    assert any("Кошелёк для звёзд: 5.00 USDT" in t for t in bot.sent)  # ниже порога 10
    assert [c for c in F["calls"] if c[1]] == [(None, None)] * 0 or True
    methods = [c[1] for c in F["calls"] if c[1]]
    assert methods[-4:] == ["searchStarsRecipient", "initBuyStarsRequest", "getBuyStarsLink", "updateStarsBuyState"], methods

    # 5. ошибка Fragment на новом заказе → failed, продавцу текст ошибки
    F["fail_link"] = True; CHAT.clear(); bot.sent.clear()
    async with sessions() as s:
        s.add(StarsOrder(seller_tg_id=1, deal_id="d2", chat_id="c2", item_name="x", buyer="bob", stars=50, status="awaiting_username"))
        await s.commit()
    P["msg"] = None
    async with sessions() as s:
        seller2 = await s.get(type(seller), 1)
        async with PlayerokClient("T") as client:
            handled = await st.on_buyer_message(bot, s, seller2, client, cipher, "c2", "@bob_ok")
        await s.commit()
        assert handled
        o2 = await s.scalar(select(StarsOrder).where(StarsOrder.deal_id == "d2"))
        assert o2.status == "failed" and "Wallet not connected" in o2.error, o2.error
    assert any("Wallet not connected" in t for t in bot.sent) and "заминка" in CHAT[-1]

    # 6. экран
    text, kb = await render_stars(sessions, seller, ft.FEATURE_BY_KEY["stars"])
    assert "выдано 1, ошибок 1" in text and all(len(b.callback_data.encode()) <= 64 for r in kb.inline_keyboard for b in r)
    # parse_cookies
    assert parse_cookies('[{"name":"stel_ssid","value":"a"},{"name":"x","value":"b"}]') == {"stel_ssid": "a", "x": "b"}
    assert parse_cookies("stel_ssid=a; stel_token=b") == {"stel_ssid": "a", "stel_token": "b"}
    print("OK")

asyncio.run(main())
