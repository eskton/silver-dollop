import asyncio
import json
import os
import sys

os.environ["BOT_TOKEN"] = "123:abc"
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ["DB_URL"] = "sqlite+aiosqlite:///:memory:"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from aiogram.types import User

from bot.config import load_settings
from bot.crypto import TokenCipher
from bot.db import init_db
from bot.handlers import build_router
from bot.playerok import AuthRequired, PlayerokClient, PlayerokError
from bot.playerok import client as client_mod
from bot.services.poller import _format_deal, _format_message, _mark_seen, sync_seller
from bot.services.sellers import get_or_create_seller

from bot.playerok.client import RawResponse, PlayerokClient as _PC


class R:
    def __init__(self, status, json=None, headers=None, text=None):
        import json as _j
        self.status = status
        self.text = text if text is not None else _j.dumps(json)
        sc = (headers or {}).get("set-cookie", "")
        self.token = sc.split("token=", 1)[1].split(";", 1)[0] if "token=" in sc else None


async def _fake_post(self, body, token):
    r = handler(body, token)
    return RawResponse(r.status, r.text, r.token)


_PC._curl_post = _fake_post


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None, disable_web_page_preview=None):
        self.sent.append((chat_id, text, reply_markup))


def handler(body, token):
    op = body["operationName"]
    if op == "getEmailAuthCode":
        return R(200, json={"data": {"getEmailAuthCode": True}})
    if op == "checkEmailAuthCode":
        if body["variables"]["input"]["code"] != "1234":
            return R(200, json={"errors": [{"message": "Неверный код"}]})
        return R(
            200,
            json={"data": {"checkEmailAuthCode": {"id": "u1", "username": "seller1", "email": "a@b.c"}}},
            headers={"set-cookie": "token=TOK123; Path=/; HttpOnly"},
        )
    assert token in ("TOK123", "TOK"), token
    if op == "viewer":
        return R(200, json={"data": {"viewer": {"id": "u1", "username": "seller1", "balance": {"available": 10.5}}}})
    if op == "deals":
        return R(200, json={"data": {"deals": {"edges": [
            {"node": {"id": "d1", "status": "PAID", "createdAt": "x", "item": {"name": "Ключ", "price": 100}, "user": {"id": "b1", "username": "buyer"}, "chat": {"id": "c1"}}}
        ]}}})
    if op == "chats":
        return R(200, json={"data": {"chats": {"edges": [
            {"node": {"id": "c1", "unreadMessagesCounter": 1, "lastMessage": {"id": "m1", "text": "привет <b>", "createdAt": "x", "user": {"id": "b1", "username": "buyer"}}}},
            {"node": {"id": "c2", "unreadMessagesCounter": 1, "lastMessage": {"id": "m2", "text": "own", "user": {"id": "u1", "username": "seller1"}}}},
        ]}}})
    if op == "createChatMessage":
        return R(200, json={"errors": [{"message": "x", "extensions": {"code": "UNAUTHENTICATED"}}]})
    raise AssertionError(op)


async def main():
    settings = load_settings()
    cipher = TokenCipher(settings.secret_key)
    assert cipher.decrypt(cipher.encrypt("hello")) == "hello"
    sessions = await init_db(settings.db_url)
    build_router()

    # Клиент на подменённом транспорте
    async with PlayerokClient() as c:
        await c.request_email_code("a@b.c")
        try:
            await c.confirm_email_code("a@b.c", "0000")
            raise AssertionError("ожидали ошибку")
        except PlayerokError as e:
            assert "Неверный код" in str(e)
        viewer = await c.confirm_email_code("a@b.c", "1234")
        assert c.token == "TOK123" and viewer.username == "seller1"
        v = await c.viewer()
        assert v.balance == 10.5
        try:
            await c.send_message("c1", "hi")
            raise AssertionError("ожидали AuthRequired")
        except AuthRequired:
            pass

        # Продавец в базе + базовый срез без уведомлений
        async with sessions() as s:
            seller = await get_or_create_seller(s, User(id=42, is_bot=False, first_name="T", username="t"))
            seller.playerok_id = "u1"
            seller.token_enc = cipher.encrypt("TOK123")
            await s.commit()
        bot = FakeBot()
        await sync_seller(bot, sessions, cipher, seller, notify=False, client=c)
        assert bot.sent == []
        await sync_seller(bot, sessions, cipher, seller, notify=True, client=c)
        assert bot.sent == [], bot.sent  # всё уже увидено

    # Новые события после базового среза приходят
    assert await _mark_seen(sessions, 42, "deal", "d2") is True
    assert await _mark_seen(sessions, 42, "deal", "d2") is False
    print(_format_deal(client_mod.Deal("d1", "PAID", "i1", "s", "Ключ", 100, "b1", "buyer", "c1", ""), "acc", 1))
    print(_format_message(client_mod.ChatPreview("c1", 1, "m1", "привет <b>", "b1", "buyer", "")))
    print("OK")


asyncio.run(main())


async def blocked():
    async def cf(body, token):
        return RawResponse(403, "<html><title>Just a moment...</title></html>", None)
    from bot.playerok import AuthRequired, PlayerokError
    async with _PC("T", transport=cf) as c:
        try:
            await c.viewer()
        except AuthRequired:
            raise AssertionError("403 от защиты не должен считаться истёкшей сессией")
        except PlayerokError as e:
            assert "защита" in str(e), e
    from bot.keyboards import is_cancel
    assert is_cancel("Отмена") and is_cancel("❌ Отмена") and is_cancel(" отмена ") and not is_cancel("a@b.c")
    print("blocked OK")

asyncio.run(blocked())
