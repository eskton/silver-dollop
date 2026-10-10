"""«Точно удалить?»: кнопки удаления сначала спрашивают подтверждение (весь бот, через диспетчер)."""
import asyncio, os, sys
from datetime import datetime
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardButton, InlineKeyboardMarkup, Message, Update, User
from sqlalchemy import func, select
from bot.crypto import TokenCipher
from bot.db import init_db, GiftcardMap, PriceRule
from bot.handlers import build_router
from bot.handlers.confirm import is_dangerous
from bot.plugins.access import OWNER_ID
from bot.plugins.giftcard import service as gc

SENT = []  # (метод, текст, кнопки)

class FakeSession(BaseSession):
    async def make_request(self, bot, method, timeout=None):
        if isinstance(method, AnswerCallbackQuery):
            SENT.append(("answer", method.text, None)); return True
        if isinstance(method, (SendMessage, EditMessageText)):
            kb = method.reply_markup
            SENT.append((type(method).__name__, method.text,
                         [b.callback_data for r in kb.inline_keyboard for b in r] if kb else None))
            return Message(message_id=99, date=datetime.now(), chat=Chat(id=OWNER_ID, type="private"), text=method.text)
        return True
    async def close(self): pass
    async def stream_content(self, *a, **k):
        if False: yield b""

USER = User(id=OWNER_ID, is_bot=False, first_name="o")
N = [0]

def click(data, screen_buttons):
    N[0] += 1
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)] for t, d in screen_buttons])
    msg = Message(message_id=10, date=datetime.now(), chat=Chat(id=OWNER_ID, type="private"), text="экран", reply_markup=kb, from_user=USER)
    return Update(update_id=N[0], callback_query=CallbackQuery(id=str(N[0]), from_user=USER, chat_instance="x", data=data, message=msg))

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    async with sessions() as s:
        s.add(GiftcardMap(seller_tg_id=OWNER_ID, lot_key="🦊 1$ STEAM GIFT CARD", category_id="c", card_id="i", quantity=3, provider="approute"))
        s.add(PriceRule(seller_tg_id=OWNER_ID, lot_key="500 робуксов", competitor_kw="*", step=1, min_price=1))
        await s.commit()
        await gc.set_api_key(s, OWNER_ID, "ar_KEY_123456", "approute")
    bot = Bot("1:x", session=FakeSession())
    dp = Dispatcher()
    dp.include_router(build_router())
    dp["sessions"] = sessions
    dp["cipher"] = TokenCipher(os.environ["SECRET_KEY"])
    screen = [("🗑 🦊 1$ STEAM GIFT CARD", "gc:del:1"), ("🗑", "gc:arkeydel")]

    # 1. случайное нажатие — ничего не удалено, только вопрос
    await dp.feed_update(bot, click("gc:del:1", screen))
    kind, text, btns = SENT[-1]
    assert kind == "SendMessage" and "Удалить «🦊 1$ STEAM GIFT CARD»?" in text, SENT[-1]
    assert btns == ["y!gc:del:1", "n!"]
    async with sessions() as s:
        assert await s.scalar(select(func.count(GiftcardMap.id))) == 1
    # 2. «Отмена» — ничего не удалено
    await dp.feed_update(bot, click("n!", []))
    assert "Отменено" in SENT[-1][1]
    async with sessions() as s:
        assert await s.scalar(select(func.count(GiftcardMap.id))) == 1
    print("1-2. нажатие на 🗑 только спрашивает; «Отмена» ничего не удаляет")

    # 3. «Да, удалить» — срабатывает обычный обработчик
    await dp.feed_update(bot, click("y!gc:del:1", []))
    async with sessions() as s:
        assert await s.scalar(select(func.count(GiftcardMap.id))) == 0
    assert any("Gift Card" in (t or "") for _, t, _ in SENT[-2:]), SENT[-2:]  # экран обновился
    print("3. «Да, удалить» — привязка удалена")

    # 4. ключ AppRoute: тоже с вопросом
    await dp.feed_update(bot, click("gc:arkeydel", screen))
    assert "Удалить «ключ AppRoute»?" in SENT[-1][1]
    async with sessions() as s:
        assert await gc.get_api_key(s, OWNER_ID, "approute") == "ar_KEY_123456"
    await dp.feed_update(bot, click("y!gc:arkeydel", []))
    async with sessions() as s:
        assert await gc.get_api_key(s, OWNER_ID, "approute") == ""
    print("4. ключ удаляется только после подтверждения")

    # 5. правило снижения цен — тот же вопрос; подделанное «y!» для безопасной кнопки игнорируется
    await dp.feed_update(bot, click("dp:del:1", [("🗑 500 робуксов", "dp:del:1")]))
    assert "Удалить «500 робуксов»?" in SENT[-1][1]
    async with sessions() as s:
        assert await s.scalar(select(func.count(PriceRule.id))) == 1
    n = len(SENT)
    await dp.feed_update(bot, click("y!gc:t", []))
    assert len(SENT) == n  # «y!» только для кнопок удаления
    for d in ("gc:del:5", "gc:keydel", "gc:arkeydel", "dp:del:3", "ar:del:1", "rl:del:2", "sr:rdel:7",
              "f:greeting:x:4", "ad:clear:abc", "cm:tdx:3:chat"):
        assert is_dangerous(d), d
    for d in ("gc:t", "gc:add", "dp:add", "mk:del:1", "f:greeting:t", "cm:tpl:c"):
        assert not is_dangerous(d), d
    print("5. все кнопки удаления в боте — с подтверждением; обычные кнопки работают как раньше")
    print("OK")
asyncio.run(main())
