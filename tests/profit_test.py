"""Калькулятор прибыли: продано шт × чистая прибыль с продажи, за 1/7/30 дней."""
import asyncio, os, re, sys, pathlib
from datetime import datetime, timedelta
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from bot.db import init_db, DealState, ProfitRule
from bot.services.analytics import profit_report
from bot.handlers.stats import parse_money

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    now = datetime.utcnow()
    def deal(i, name, hours_ago, status="CONFIRMED"):
        return DealState(seller_tg_id=1, deal_id=f"d{i}", item_name=name, price=100, status=status,
                         created_at=now - timedelta(hours=hours_ago), first_seen_at=now)
    LOT = "💰 50 РОБУКСОВ | ПРОМОКОД | ВЫДАЧА ЗА 1 СЕК 💎"
    async with sessions() as s:
        s.add_all([
            deal(1, LOT, 2), deal(2, LOT, 5),                 # 1 день
            deal(3, LOT, 30),                                 # 7 дней
            deal(4, LOT, 24 * 20),                            # 30 дней
            deal(5, LOT, 24 * 40),                            # старше 30 дней
            deal(6, LOT, 1, status="ROLLBACK"),               # возврат — не считаем
            deal(7, "150 робуксов", 1),                       # «50 робуксов» не цепляет «150 робуксов»
        ])
        # пользователь вставил полное название лота с эмодзи
        s.add(ProfitRule(seller_tg_id=1, keyword=LOT, cost=0.05, currency="$"))
        s.add(ProfitRule(seller_tg_id=1, keyword="50 робуксов", cost=5, currency="₽"))
        await s.commit()
        text = await profit_report(s, 1, now)
    print(text)
    b1, b2 = text.split("🔑")[1], text.split("🔑")[2]
    assert "0.05$ с продажи" in b1
    assert "1 день: 2 шт → <b>0.1$</b>" in b1, b1
    assert "7 дней: 3 шт → <b>0.15$</b>" in b1, b1
    assert "30 дней: 4 шт → <b>0.2$</b>" in b1, b1
    assert "30 дней: 4 шт → <b>20 ₽</b>" in b2, b2
    assert "омисси" not in text
    assert parse_money("0.05$") == (0.05, "$") and parse_money("$0,05") == (0.05, "$")
    assert parse_money("5₽") == (5, "₽") and parse_money("5 руб") == (5, "₽") and parse_money("0.05") == (0.05, "$")
    assert parse_money("abc") is None and parse_money("-1$") is None
    # кнопки калькулятора не пересекаются с другими экранами
    root = pathlib.Path(__file__).resolve().parents[1] / "bot"
    others = "".join(p.read_text() for p in root.rglob("*.py") if p.name != "stats.py")
    assert not re.search(r'"pc[:"]', others)
    print("OK")
asyncio.run(main())
