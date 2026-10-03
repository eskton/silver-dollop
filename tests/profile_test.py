import asyncio, os, sys
from datetime import datetime, timedelta
from types import SimpleNamespace
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.db import init_db, DealState
from bot.services import notifications as nt, features as ft
from bot.services.analytics import build_report
from bot.handlers import account

class Bot:
    def __init__(self): self.sent = []
    async def send_message(self, chat_id, text, reply_markup=None, disable_notification=None, disable_web_page_preview=None):
        self.sent.append((text, disable_notification))

class Msg:
    def __init__(self): self.edits = []; self.answers = []
    async def edit_text(self, text, reply_markup=None, **kw): self.edits.append((text, reply_markup))
    async def answer(self, text, reply_markup=None, **kw): self.answers.append(text)

class CB:
    def __init__(self, data): self.data = data; self.from_user = SimpleNamespace(id=1, username="u"); self.message = Msg()
    async def answer(self, *a, **kw): pass

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    bot = Bot()
    async with sessions() as s:
        assert await nt.notify(bot, s, 1, "deal", "a")
        assert bot.sent[-1] == ("a", False)                 # со звуком по умолчанию
        assert await nt.notify(bot, s, 1, "system", "b")
        assert bot.sent[-1] == ("b", True)                  # системные — без звука
        await nt.toggle(s, 1, "deal", "on")
        assert not await nt.notify(bot, s, 1, "deal", "c")  # выключено
        await nt.toggle(s, 1, "message", "sound")
        await nt.notify(bot, s, 1, "message", "d")
        assert bot.sent[-1] == ("d", True)
        assert await ft.get_tz(s, 1) == 3
        now = datetime(2026, 10, 2, 22, 30)  # 01:30 следующего дня по МСК
        s.add_all([
            DealState(seller_tg_id=1, deal_id="x1", item_name="A", price=100, status="COMPLETED",
                      buyer="bob", created_at=datetime(2026, 10, 2, 21, 10), first_seen_at=now),  # 00:10 МСК — «сегодня»
            DealState(seller_tg_id=1, deal_id="x2", item_name="B", price=50, status="PAID",
                      buyer="bob", chat_id="c2", created_at=datetime(2026, 10, 2, 20, 0), first_seen_at=now),  # 23:00 МСК — «вчера»
            DealState(seller_tg_id=1, deal_id="x3", item_name="C", price=70, status="SENT",
                      buyer="amy", chat_id="c3", created_at=datetime(2026, 9, 30), first_seen_at=now),
        ])
        await s.commit()
        r = await build_report(s, 1, now=now)
        assert "<b>Сегодня:</b> 1 зак. · 100 ₽" in r, r
        await ft.set_setting(s, 1, "tz", "0")
        r = await build_report(s, 1, now=now)
        assert "<b>Сегодня:</b> 2 зак. · 150 ₽" in r, r
        await ft.set_setting(s, 1, "tz", "3")

    # экраны профиля
    for data in ["pf:notify", "n:deal:on", "n:review:sound", "pf:orders", "pf:clients", "pf:tz", "tz:7"]:
        cb = CB(data)
        handler = {
            "pf:notify": account.notify_cb, "pf:orders": account.orders_cb, "pf:clients": account.clients_cb,
            "pf:tz": account.tz_cb,
        }.get(data) or (account.notify_toggle if data.startswith("n:") else account.tz_set)
        await handler(cb, sessions)
        text, kb = cb.message.edits[-1]
        for row in kb.inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64, b.callback_data
        print(f"--- {data}\n{text}\n{[[b.text for b in r] for r in kb.inline_keyboard]}")
    async with sessions() as s:
        assert await ft.get_tz(s, 1) == 7
        on, _ = await nt.get_prefs(s, 1, "deal")
        assert on  # было выключено в начале, тумблер в экране включил обратно
        _, sound = await nt.get_prefs(s, 1, "review")
        assert not sound
    print("OK")

asyncio.run(main())
