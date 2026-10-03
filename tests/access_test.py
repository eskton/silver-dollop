import asyncio, os, sys
from datetime import datetime, timedelta
from types import SimpleNamespace
os.environ["ADMIN_IDS"] = "777"
os.environ["OWNER_CONTACT"] = "@eskton"
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.db import init_db, Seller
from bot.plugins import access as ac
from bot.plugins.stars.handlers import render_stars
from bot.plugins.stars import service as st
from bot.services import features as ft
from bot.services.sellers import get_or_create_seller
from bot.handlers import admin
from bot.handlers.settings import settings_menu_kb

class Msg:
    def __init__(self, uid, text=""):
        self.from_user = SimpleNamespace(id=uid, username="adm"); self.text = text; self.out = []
        self.bot = SimpleNamespace(send_message=self._send)
    async def answer(self, text, **kw): self.out.append(text)
    async def _send(self, chat_id, text, **kw): self.out.append(("dm", chat_id, text))

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    async with sessions() as s:
        u = await get_or_create_seller(s, SimpleNamespace(id=1, username="vasya"))
        u.playerok_id = "p1"; u.token_enc = "x"; await s.commit()
        await ft.set_setting(s, 1, "stars_enabled", "1")
        assert ac.is_admin(777) and not ac.is_admin(1)
        assert await ac.has_access(s, 777, "stars") and not await ac.has_access(s, 1, "stars")
        # экран с замком и замок в меню
        text, kb = await render_stars(sessions, u, ft.FEATURE_BY_KEY["stars"])
        assert "🔒" in text and "@eskton" in text and len(kb.inline_keyboard) == 1
        menu = await settings_menu_kb(sessions, 1)
        assert any("Звёзды" in b.text and "🔒" in b.text for r in menu.inline_keyboard for b in r)
        # без доступа автоматика не берёт заказ звёзд
        deal = SimpleNamespace(id="d", chat_id="c", item_name="500 звёзд", buyer_username="b")
        assert not await st.on_paid(None, s, u, None, deal)
    # админ выдаёт на 30 дней
    m = Msg(777); await admin.grant_cmd(m, SimpleNamespace(args="@vasya stars 30"), sessions)
    assert "✅" in m.out[0] and "до" in m.out[0] and m.out[1][0] == "dm", m.out
    async with sessions() as s:
        assert await ac.has_access(s, 1, "stars")
        row = await ac.get_access(s, 1, "stars"); assert row.expires_at and row.expires_at > datetime.utcnow() + timedelta(days=29)
        menu = await settings_menu_kb(sessions, 1)
        assert not any("🔒" in b.text for r in menu.inline_keyboard for b in r)
        # срок истёк
        row.expires_at = datetime.utcnow() - timedelta(minutes=1); await s.commit()
        assert not await ac.has_access(s, 1, "stars")
    # бессрочно по id, список, отзыв
    m = Msg(777); await admin.grant_cmd(m, SimpleNamespace(args="1 stars"), sessions); assert "бессрочно" in m.out[0]
    m = Msg(777); await admin.access_cmd(m, SimpleNamespace(args=""), sessions); assert "vasya" in m.out[0] and "бессрочно" in m.out[0]
    m = Msg(777); await admin.revoke_cmd(m, SimpleNamespace(args="1 stars"), sessions); assert "отозван" in m.out[0]
    async with sessions() as s:
        assert not await ac.has_access(s, 1, "stars")
    m = Msg(777); await admin.grant_cmd(m, SimpleNamespace(args="@nobody stars"), sessions); assert "Не нашёл" in m.out[0]
    m = Msg(777); await admin.grant_cmd(m, SimpleNamespace(args="1 foo"), sessions); assert "Нет такого плагина" in m.out[0]
    print("OK")
asyncio.run(main())
