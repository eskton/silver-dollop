"""Кнопки под уведомлением как в Easy Sell: Ответ / Шаблоны / Весь чат / Подтвердить / Возврат."""
import asyncio, json, os, sys
from datetime import datetime, timedelta
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select, func
from bot.crypto import TokenCipher
from bot.db import init_db, DealState, QuickReply
from bot.keyboards import deal_kb
from bot.playerok.client import PlayerokClient, RawResponse
from bot.services.sellers import get_or_create_seller
from bot.handlers import chat as ch

CALLS = []
MSGS = [  # Playerok отдаёт свежие первыми
    {"id": "m3", "text": "", "createdAt": "2026-10-08T10:05:00.000Z", "user": {"id": "b1", "username": "Max"}, "images": [{"id": "x"}]},
    {"id": "m2", "text": "Держи код <ABC>", "createdAt": "2026-10-08T10:02:00.000Z", "user": {"id": "me", "username": "eskton"}},
    {"id": "m1", "text": "Привет", "createdAt": "2026-10-08T10:00:00.000Z", "user": {"id": "b1", "username": "Max"}},
]
async def pk(body, token):
    op, v = body["operationName"], body["variables"]
    CALLS.append((op, v))
    if op == "updateDeal":
        return RawResponse(200, json.dumps({"data": {"updateDeal": {"id": v["input"]["id"], "status": v["input"]["status"]}}}), None)
    if op == "createChatMessage":
        return RawResponse(200, json.dumps({"data": {"createChatMessage": {"id": "new"}}}), None)
    if op == "chatMessages":
        assert v["filter"] == {"chatId": "c1"}, v
        return RawResponse(200, json.dumps({"data": {"chatMessages": {"edges": [{"node": m} for m in MSGS]}}}), None)
    raise AssertionError(op)
PlayerokClient._curl_post = lambda self, body, token: pk(body, token)

OUT, ALERTS = [], []
class Msg:
    def __init__(self, text=""): self.text = text; self.from_user = USER
    async def edit_text(self, text, reply_markup=None, **k): OUT.append((text, reply_markup))
    async def edit_reply_markup(self, reply_markup=None): pass
    async def answer(self, text, reply_markup=None, **k): OUT.append((text, reply_markup))
USER = SimpleNamespace(id=1, username="u", first_name="u")
class Cb:
    def __init__(self, data, user=USER): self.data = data; self.from_user = user; self.message = Msg()
    async def answer(self, text=None, show_alert=False, **k):
        if show_alert: ALERTS.append(text)

def buttons(kb):
    return [b.callback_data for r in kb.inline_keyboard for b in r]

async def main():
    # --- клавиатура ---
    kb = deal_kb("c1")
    assert [[b.text for b in r] for r in kb.inline_keyboard] == [
        ["✉️ Ответ", "📋 Шаблоны", "💬 Весь чат"], ["✅ Подтвердить", "↩️ Возврат"]]
    assert buttons(kb) == ["reply:c1", "cm:tpl:c1", "cm:all:c1", "cm:ok:cc1", "cm:rf:cc1"]
    assert buttons(deal_kb("c1", deal_id="d1"))[3:] == ["cm:ok:dd1", "cm:rf:dd1"]
    assert buttons(deal_kb("c1", actions=False)) == ["reply:c1", "cm:tpl:c1", "cm:all:c1"]
    uuid = "11111111-2222-3333-4444-555555555555"
    assert all(len(d.encode()) <= 64 for d in buttons(deal_kb(uuid, uuid, uuid)))
    print("1. кнопки как в Easy Sell")

    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    cipher = TokenCipher(os.environ["SECRET_KEY"])
    now = datetime.utcnow()
    async with sessions() as s:
        seller = await get_or_create_seller(s, USER)
        seller.playerok_id = "me"; seller.playerok_username = "eskton"; seller.token_enc = cipher.encrypt("T")
        s.add(DealState(seller_tg_id=1, deal_id="d0", chat_id="c1", item_name="Старый", status="CONFIRMED",
                        buyer="Max", created_at=now - timedelta(days=3)))
        s.add(DealState(seller_tg_id=1, deal_id="d1", chat_id="c1", item_name="50 робуксов", status="PAID",
                        buyer="Max", price=90, created_at=now))
        await s.commit()

    # --- ✅ Подтвердить: по чату находит активный заказ, сначала спрашивает ---
    await ch.confirm_ask(Cb("cm:ok:cc1"), sessions)
    text, kb = OUT[-1]
    assert "Отметить заказ выполненным" in text and "50 робуксов" in text and "90 ₽" in text, text
    assert buttons(kb) == ["cm:oky:dd1", "cm:no"], buttons(kb)
    assert not CALLS
    await ch.confirm_do(Cb("cm:oky:dd1"), sessions, cipher)
    assert CALLS[-1] == ("updateDeal", {"input": {"id": "d1", "status": "SENT"}}), CALLS
    assert "отмечен выполненным" in OUT[-1][0]
    print("2. подтверждение — с вопросом, по нужному заказу")

    async with sessions() as s:  # опрос увидел новый статус
        st = await s.scalar(select(DealState).where(DealState.deal_id == "d1"))
        st.status = "SENT"; await s.commit()
    n = len(CALLS)
    await ch.confirm_ask(Cb("cm:ok:cc1"), sessions)
    assert "уже отмечен" in ALERTS[-1] and len(CALLS) == n
    await ch.refund_ask(Cb("cm:rf:dd0"), sessions)
    assert "уже подтвердил" in ALERTS[-1]
    await ch.refund_do(Cb("cm:rfy:dd0"), sessions, cipher)  # и в обход вопроса — тоже нет
    assert len(CALLS) == n
    print("3. закрытые заказы не трогает")

    # --- ↩️ Возврат ---
    await ch.refund_ask(Cb("cm:rf:cc1"), sessions)
    text, kb = OUT[-1]
    assert "Вернуть деньги" in text and "50 робуксов" in text and buttons(kb) == ["cm:rfy:dd1", "cm:no"]
    await ch.refund_do(Cb("cm:rfy:dd1"), sessions, cipher)
    assert CALLS[-1] == ("updateDeal", {"input": {"id": "d1", "status": "ROLLED_BACK"}}), CALLS[-1]
    assert "Возврат оформлен" in OUT[-1][0]
    # чужой продавец не может вернуть чужой заказ
    other = SimpleNamespace(id=2, username="x", first_name="x")
    n = len(CALLS)
    await ch.refund_do(Cb("cm:rfy:dd1", other), sessions, cipher)
    assert len(CALLS) == n and ALERTS[-1] == "Заказ не найден."
    print("4. возврат — с вопросом; чужие заказы недоступны")

    # --- 📋 Шаблоны ---
    await ch.quick_menu(Cb("cm:tpl:c1"), sessions)
    assert "Шаблонов пока нет" in OUT[-1][0] and buttons(OUT[-1][1]) == ["cm:tadd:c1"]
    state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await ch.quick_add(Cb("cm:tadd:c1"), state, sessions)
    assert await state.get_state() == ch.QuickAdd.text.state
    await ch.quick_save(Msg("{Имя_Клиента}, код уже в чате, проверь пожалуйста и подтверди заказ 🙏"), state, sessions)
    assert await state.get_state() is None
    text, kb = OUT[-1]
    assert buttons(kb)[0].startswith("cm:ts:") and buttons(kb)[0].endswith(":c1")
    title = kb.inline_keyboard[0][0].text
    assert title.startswith("📨 {Имя_Клиента}, код") and len(title) <= 33, title
    await ch.quick_send(Cb(buttons(kb)[0]), sessions, cipher)
    assert CALLS[-1] == ("createChatMessage", {"input": {"chatId": "c1", "text": "Max, код уже в чате, проверь пожалуйста и подтверди заказ 🙏"}}), CALLS[-1]
    assert "Отправлено" in OUT[-1][0]
    # чужой шаблон не отправить
    n = len(CALLS)
    async with sessions() as s:
        other_seller = await get_or_create_seller(s, other)
        other_seller.playerok_id = "o"; other_seller.token_enc = cipher.encrypt("T2"); await s.commit()
    await ch.quick_send(Cb(buttons(kb)[0], other), sessions, cipher)
    assert len(CALLS) == n and ALERTS[-1] == "Шаблон удалён."
    await ch.quick_del_menu(Cb("cm:tdl:c1"), sessions)
    dl = buttons(OUT[-1][1])
    assert dl[0].startswith("cm:tdx:") and dl[-1] == "cm:tbk:c1"
    await ch.quick_del(Cb(dl[0]), sessions)
    async with sessions() as s:
        assert await s.scalar(select(func.count(QuickReply.id))) == 0
    assert "Шаблонов пока нет" in OUT[-1][0]
    print("5. шаблоны: добавить, отправить с переменными, удалить")

    # --- 💬 Весь чат ---
    await ch.chat_history(Cb("cm:all:c1"), sessions, cipher)
    text, kb = OUT[-1]
    print(text)
    assert CALLS[-1][0] == "chatMessages"
    i1, i2, i3 = text.index("Привет"), text.index("Держи код &lt;ABC&gt;"), text.index("картинка")
    assert i1 < i2 < i3, text  # от старых к новым
    assert "🟢 Вы" in text and "🔵 Max" in text and "08.10 13:00" in text  # МСК по умолчанию
    assert buttons(kb) == ["reply:c1", "cm:tpl:c1"]
    print("6. весь чат — по порядку, свои отмечены")
    print("OK")
asyncio.run(main())
