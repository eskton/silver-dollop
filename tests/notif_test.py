import asyncio, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.db import init_db, SeenEvent
from bot.services.poller import _format_deal, _format_message, _daily_order_number, _mark_seen
from bot.playerok.client import Deal, ChatPreview

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    deal = Deal(id="d1", status="PAID", item_id="i1", item_slug="robux-50", item_name="50 РОБУКСОВ | ПРОМОКОД",
                price=90, buyer_id="b1", buyer_username="Malisonlif", chat_id="c1", created_at="2026-10-02T15:36:00Z")
    for i in range(3):
        await _mark_seen(sessions, 1, "deal", f"d{i}")
    n = await _daily_order_number(sessions, 1)
    txt = _format_deal(deal, "eskton", n)
    print(txt); print("---")
    assert "Заказ №3 за сегодня для eskton" in txt
    assert '<a href="https://playerok.com/products/robux-50">50 РОБУКСОВ | ПРОМОКОД</a>' in txt
    assert '<a href="https://playerok.com/deal/d1">Открыть заказ</a>' in txt
    assert '<a href="https://playerok.com/chats/c1">Открыть чат</a>' in txt
    chat = ChatPreview(id="c9", unread=1, last_message_id="m1", last_text="привет", last_author_id="b1", last_author_username="Максим Ж.", created_at="")
    mt = _format_message(chat); print(mt)
    assert "Новое сообщение от Максим Ж." in mt and "Открыть чат" in mt
    print("OK")
asyncio.run(main())
