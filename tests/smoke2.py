import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

os.environ["BOT_TOKEN"] = "123:abc"
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ["DB_URL"] = "sqlite+aiosqlite:///:memory:"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from aiogram.types import User
from sqlalchemy import select

from bot.config import load_settings
from bot.crypto import TokenCipher
from bot.db import AutoReply, DealState, DeliveryItem, init_db
from bot.handlers import build_router
from bot.handlers.settings import render_feature
from tests.fakes.tariffs import check_publish, tariff_response
from bot.playerok import PlayerokClient
from bot.playerok import client as client_mod
from bot.services import automation, features as ft
from bot.services.poller import sync_seller
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

SENT_TO_CHAT = []
STATE = {"deal_status": "PAID", "review": None, "msg": None, "unread": 0, "confirmed": []}


def handler(body, token):
    op, v = body["operationName"], body["variables"]
    _t = tariff_response(op, v)
    if _t: return _t
    if op == "publishItem": check_publish(v)
    if op == "deals":
        assert "status" not in v["filter"]
        node = {"id": "d1", "status": STATE["deal_status"], "createdAt": "x",
                "item": {"id": "i1", "slug": "key-1", "name": "Ключ Steam", "price": 100},
                "user": {"id": "b1", "username": "buyer"}, "chat": {"id": "c1"}}
        if STATE["review"]:
            node["review"] = STATE["review"]
        return R(200, json={"data": {"deals": {"edges": [{"node": node}]}}})
    if op == "chats":
        edges = []
        if STATE["msg"]:
            edges.append({"node": {"id": "c1", "unreadMessagesCounter": STATE["unread"],
                                   "lastMessage": {"id": STATE["msg"][0], "text": STATE["msg"][1], "user": {"id": "b1", "username": "buyer"}}}})
        return R(200, json={"data": {"chats": {"edges": edges}}})
    if op == "createChatMessage":
        SENT_TO_CHAT.append(v["input"]["text"])
        return R(200, json={"data": {"createChatMessage": {"id": "m"}}})
    if op == "updateDeal":
        STATE["confirmed"].append(v["input"])
        STATE["deal_status"] = "SENT"
        return R(200, json={"data": {"updateDeal": {"id": "d1", "status": "SENT"}}})
    if op == "items":
        return R(200, json={"data": {"items": {"edges": [
            {"node": {"id": "i1", "name": "Ключ Steam", "status": "APPROVED"}},
            {"node": {"id": "i2", "name": "Старый", "status": "SOLD"}},
            {"node": {"id": "i3", "name": "Платный", "status": "SOLD", "priorityStatus": {"price": 50}}},
            {"node": {"id": "i4", "name": "Другой", "status": "SOLD"}}]}}})
    if op in ("increaseItemPriorityStatus", "publishItem"):
        STATE.setdefault(op, []).append(v["input"]["itemId"])
        return R(200, json={"data": {op: {"id": v["input"]["itemId"]}}})
    raise AssertionError(op)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None, disable_web_page_preview=None):
        self.sent.append(text)


async def main():
    settings = load_settings()
    cipher = TokenCipher(settings.secret_key)
    sessions = await init_db(settings.db_url)
    build_router()

    async with sessions() as s:
        seller = await get_or_create_seller(s, User(id=42, is_bot=False, first_name="T", username="t"))
        seller.playerok_id, seller.playerok_username = "u1", "eskton"
        seller.token_enc = cipher.encrypt("TOK")
        await s.commit()
        tg = seller.tg_id
        await ft.set_setting(s, tg, "autoconfirm_enabled", "1")
        await ft.set_setting(s, tg, "after_buyer_confirm_enabled", "1")
        await ft.set_setting(s, tg, "after_review_enabled", "1")
        await ft.set_setting(s, tg, "ignore_enabled", "1")
        await ft.set_setting(s, tg, "ignore_minutes", "1")
        await ft.set_setting(s, tg, "confirm_reminder_enabled", "1")
        await ft.set_setting(s, tg, "bump_enabled", "1")
        await ft.set_setting(s, tg, "relist_enabled", "1")
        await ft.set_setting(s, tg, "bump_daily_limit", "1")
        await ft.set_setting(s, tg, "relist_all", "0")
        from bot.db import RelistRule
        s.add(RelistRule(seller_tg_id=tg, pattern="стар"))
        s.add(RelistRule(seller_tg_id=tg, pattern="платн"))
        await ft.set_template(s, tg, "greeting", "Привет {Имя_Клиента}, лот {Название_Лота} за {Цена}, {Ссылка}, {Аккаунт}")
        s.add(DeliveryItem(seller_tg_id=tg, item_name="ключ steam", item_key="ключ steam", content="KEY-AAA"))
        s.add(DeliveryItem(seller_tg_id=tg, item_name="Ключ Steam", item_key="ключ steam", content="KEY-BBB"))
        s.add(AutoReply(seller_tg_id=tg, keyword="когда", text="Скоро, {Имя_Клиента}!"))
        await s.commit()

    bot = FakeBot()
    # Круг 1: новый оплаченный заказ → приветствие, выдача, автоподтверждение
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    print("chat:", SENT_TO_CHAT)
    print("tg:", bot.sent)
    assert SENT_TO_CHAT[0] == "Привет buyer, лот Ключ Steam за 100 ₽, https://playerok.com/products/key-1, eskton"
    assert "KEY-AAA" in SENT_TO_CHAT[1]
    assert STATE["confirmed"] == [{"id": "d1", "status": "SENT"}]
    assert any("Автоподтверждение" in t for t in bot.sent) and any("Осталось: 1" in t for t in bot.sent)
    assert STATE["increaseItemPriorityStatus"] == ["i1"] and STATE["publishItem"] == ["i2"]
    n = len(SENT_TO_CHAT)

    # Круг 2: ничего нового — ничего не шлём
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert len(SENT_TO_CHAT) == n

    # Покупатель написал "когда?" → автоответчик
    STATE["msg"], STATE["unread"] = ("m1", "Когда выдадите?"), 1
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert SENT_TO_CHAT[-1] == "Скоро, buyer!"
    # Другое сообщение без ключевого слова, висит дольше минуты → текст при игноре
    STATE["msg"] = ("m2", "ау")
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    n = len(SENT_TO_CHAT)
    async with sessions() as s:
        from bot.db import ChatState
        cs = await s.scalar(select(ChatState).where(ChatState.chat_id == "c1"))
        cs.last_buyer_message_at = datetime.utcnow() - timedelta(minutes=5)
        ds = await s.scalar(select(DealState).where(DealState.deal_id == "d1"))
        ds.sent_at = datetime.utcnow() - timedelta(minutes=61)
        await s.commit()
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert sorted(SENT_TO_CHAT[-2:]) == sorted(["Извините за задержку, скоро отвечу!", "Здравствуйте! Если вы уже получили товар и всё в порядке, пожалуйста, подтвердите получение заказа: https://playerok.com/deal/d1"]), SENT_TO_CHAT[n:]
    STATE["msg"] = None
    # Цикличное напоминание: повтор через 180 мин
    async with sessions() as s:
        ds = await s.scalar(select(DealState).where(DealState.deal_id == "d1"))
        assert ds.confirm_reminders == 1
        ds.confirm_reminded_at = datetime.utcnow() - timedelta(minutes=181)
        await s.commit()
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert SENT_TO_CHAT[-1].startswith("Здравствуйте!"), SENT_TO_CHAT[-1]

    # Покупатель подтвердил, потом отзыв
    STATE["deal_status"] = "COMPLETED"
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert SENT_TO_CHAT[-1] == "Спасибо за покупку, buyer! Буду рад отзыву 😊"
    STATE["review"] = {"rating": 5, "text": "топ"}
    await sync_seller(bot, sessions, cipher, seller, notify=True)
    assert SENT_TO_CHAT[-1] == "Спасибо за отзыв! 🙏" and "⭐⭐⭐⭐⭐" in bot.sent[-1]

    # Экраны настроек рендерятся
    for f in ft.FEATURES:
        text, kb = await render_feature(sessions, seller, f)
        assert f.title in text and all(len(b.callback_data) <= 64 for r in kb.inline_keyboard for b in r)
    print("OK")


asyncio.run(main())
