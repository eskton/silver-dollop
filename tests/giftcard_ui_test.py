"""/giftcard → «Привязать лот»: категория и номинал выбираются кнопками, без ввода ID."""
import asyncio, json, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
os.environ["FAZER_API_KEY"] = "fc_TEST_KEY_123"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy import select
from bot.db import init_db, GiftcardMap
from bot.plugins.access import OWNER_ID
from bot.plugins.giftcard import handlers as h, service as gc

CATS = [{"category_id": f"cat-{i}", "name": f"Card {i}"} for i in range(40)] + [{"category_id": "steam-us", "name": "Steam USA"}]
async def fazer(method, url, headers, params, body):
    path = url.split("api.fzr.cards", 1)[1]
    if path == "/api/v2/giftcards":
        return 200, json.dumps({"ok": True, "kind": "gift_card", "items": CATS, "meta": {"total": len(CATS), "limit": 500, "next_cursor": None, "has_more": False}})
    if path == "/api/v2/giftcards/cards":
        assert params["category_id"] == "steam-us", params
        return 200, json.dumps({"ok": True, "kind": "gift_card", "category_id": "steam-us", "name": "Steam USA", "offers": [
            {"card_id": "s5", "name": "$5", "price_usd": "5.2", "stock": 9, "min_order_quantity": 1, "max_order_quantity": 10},
            {"card_id": "s10", "name": "$10", "price_usd": "10.3", "stock": 4, "min_order_quantity": 1, "max_order_quantity": 10}]})
    raise AssertionError(path)
gc.TRANSPORT = fazer

USER = SimpleNamespace(id=OWNER_ID, username="o", first_name="o", last_name=None, language_code="ru", is_bot=False)
SENT = []
class Msg:
    def __init__(self, text=""): self.text = text; self.from_user = USER
    async def answer(self, text, reply_markup=None, **k): SENT.append((text, reply_markup))
class Cb:
    def __init__(self, data): self.data = data; self.from_user = USER; self.message = Msg()
    async def answer(self, *a, **k): pass

def buttons():
    kb = SENT[-1][1]
    return [(b.text, b.callback_data) for r in kb.inline_keyboard for b in r] if hasattr(kb, "inline_keyboard") else []

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=OWNER_ID, user_id=OWNER_ID))
    await h.add_start(Cb("gc:add"), state)
    await h.add_lot(Msg("💰 100 РОБУКСОВ | ПРОМОКОД | ВЫДАЧА ЗА 1 СЕК 💎"), state, sessions)
    assert "Шаг 2/4" in SENT[-1][0] and "первые 30 из 41" in SENT[-1][0], SENT[-1][0]
    assert len(buttons()) == 30
    await h.add_category_search(Msg("steam"), state)          # поиск по названию
    assert buttons() == [("Steam USA", "gc:pc:0")], buttons()
    await h.add_category_pick(Cb("gc:pc:0"), state, sessions)
    assert "Шаг 3/4" in SENT[-1][0]
    assert [b[1] for b in buttons()] == ["gc:po:0", "gc:po:1"] and "$10" in buttons()[1][0]
    await h.add_card_pick(Cb("gc:po:1"), state)
    assert "Шаг 4/4" in SENT[-1][0] and buttons()[0] == ("1", "gc:pq:1")
    await h.add_quantity_pick(Cb("gc:pq:1"), state, sessions)
    async with sessions() as s:
        m = await s.scalar(select(GiftcardMap))
    assert (m.lot_key, m.category_id, m.card_id, m.quantity) == (
        "💰 100 РОБУКСОВ | ПРОМОКОД | ВЫДАЧА ЗА 1 СЕК 💎", "steam-us", "s10", 1), m
    assert await state.get_state() is None
    assert any("✅ Привязано" in t for t, _ in SENT)
    assert all(len(x.callback_data.encode()) <= 64
               for t, kb in SENT if kb is not None and hasattr(kb, "inline_keyboard")
               for r in kb.inline_keyboard for x in r)
    print("OK")
asyncio.run(main())
