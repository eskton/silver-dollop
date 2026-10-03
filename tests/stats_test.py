import asyncio, os, sys, sqlite3, tempfile
from datetime import datetime, timedelta
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.db import init_db, DealState, DeliveryItem, ActionLog
from bot.services.analytics import build_report
from bot.services.automation import _parse_dt

async def main():
    # 1) старая база без новых колонок
    path = tempfile.mktemp(suffix=".db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE deal_states (id INTEGER PRIMARY KEY, seller_tg_id BIGINT, deal_id VARCHAR(128), chat_id VARCHAR(128), item_name VARCHAR(255), status VARCHAR(32), first_seen_at DATETIME, sent_at DATETIME, confirmed_at DATETIME, delivered BOOLEAN, confirm_reminded_at DATETIME, confirm_reminders INTEGER, review_reminded BOOLEAN, review_thanked BOOLEAN)")
    con.execute("INSERT INTO deal_states (seller_tg_id, deal_id, item_name, status, first_seen_at, delivered) VALUES (1, 'old', 'Старый', 'COMPLETED', '2026-09-01 10:00:00', 0)")
    con.commit(); con.close()
    sessions = await init_db(f"sqlite+aiosqlite:///{path}")
    cols = [r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(deal_states)")]
    assert {"price", "buyer", "created_at", "review_rating"} <= set(cols), cols

    now = datetime(2026, 10, 2, 15, 0)
    async with sessions() as s:
        def d(i, name, price, status, days, buyer="b", rating=None, delivered=False):
            return DealState(seller_tg_id=1, deal_id=f"d{i}", item_name=name, price=price, status=status,
                             created_at=now - timedelta(days=days), buyer=buyer, review_rating=rating,
                             delivered=delivered, first_seen_at=now)
        s.add_all([
            d(1, "Ключ Steam", 100, "PAID", 0, "a", delivered=True),
            d(2, "Ключ Steam", 100, "COMPLETED", 0.2, "b", 5, True),
            d(3, "Аккаунт", 500, "SENT", 3, "c"),
            d(4, "Аккаунт", 500, "COMPLETED", 20, "c", 2),
            d(5, "Буст", 1000, "ROLLBACK", 1, "e"),
            d(6, "Буст", 300, "CONFIRMED", 60, "f", 4),
        ])
        s.add_all([DeliveryItem(seller_tg_id=1, item_name="Ключ Steam", item_key="ключ steam", content="x", used_deal_id="d1"),
                   DeliveryItem(seller_tg_id=1, item_name="Ключ Steam", item_key="ключ steam", content="y")])
        s.add(ActionLog(seller_tg_id=1, kind="bump", target="i1", cost=5))
        await s.commit()
        report = await build_report(s, 1, now=now)
    print(report)
    assert "<b>Сегодня:</b> 2 зак. · 200 ₽" in report
    assert "<b>7 дней:</b> 3 зак. · 700 ₽" in report
    assert "<b>30 дней:</b> 4 зак. · 1 200 ₽" in report
    assert "проблемы и возвраты: 1" in report and "ждут выдачи: 1" in report
    assert "1. Аккаунт — 2 шт. · 1 000 ₽" in report
    assert "Отзывы:</b> 3 шт., средняя оценка 3.7" in report
    assert "выдано автовыдачей: 2" in report and "поднятий лотов: 1 (потрачено 5 ₽)" in report
    assert "Ключ Steam — осталось 1" in report
    async with sessions() as s:
        assert "Пока нет данных" in await build_report(s, 999)
    assert _parse_dt("2026-10-02T09:18:07.067Z") == datetime(2026, 10, 2, 9, 18, 7, 67000)
    assert _parse_dt("2026-10-02T12:00:00+03:00") == datetime(2026, 10, 2, 9, 0)
    assert _parse_dt("") is None and _parse_dt("мусор") is None
    print("OK")

asyncio.run(main())
