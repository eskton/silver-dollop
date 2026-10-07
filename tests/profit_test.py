"""Калькулятор прибыли по ключевым словам: 1/7/30 дней."""
import asyncio, os, sys
from datetime import datetime, timedelta
os.environ["SECRET_KEY"] = __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from bot.db import init_db, DealState, ProfitRule
from bot.services import features as ft
from bot.services.analytics import PROFIT_FEE_KEY, profit_report
from bot.handlers.stats import _num

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    now = datetime.utcnow()
    def deal(i, name, price, hours_ago, status="CONFIRMED"):
        return DealState(seller_tg_id=1, deal_id=f"d{i}", item_name=name, price=price, status=status,
                         created_at=now - timedelta(hours=hours_ago), first_seen_at=now)
    async with sessions() as s:
        s.add_all([
            deal(1, "💰 100 РОБУКСОВ | ПРОМОКОД 💎", 100, 2),         # 1 день
            deal(2, "✅ДЛЯ РФ✅100 робуксов по коду", 120, 30),        # 7 дней
            deal(3, "100 Робуксов", 100, 24 * 20),                    # 30 дней
            deal(4, "100 робуксов", 100, 24 * 40),                    # старше 30 дней
            deal(5, "100 робуксов", 100, 1, status="ROLLBACK"),       # возврат — не считаем
            deal(6, "500 робуксов", 400, 1),                          # другое слово
            deal(7, "2100 робуксов", 700, 1),                         # «100 робуксов» не цепляет «2100 робуксов»
        ])
        s.add(ProfitRule(seller_tg_id=1, keyword="100 робуксов", cost=60))
        s.add(ProfitRule(seller_tg_id=1, keyword="500 РОБУКСОВ", cost=300))
        await s.commit()
        await ft.set_setting(s, 1, PROFIT_FEE_KEY, "10")
        text = await profit_report(s, 1, now)
    print(text)
    block = text.split("🔑")[1]
    # 1 день: 1 шт, 100 ₽, прибыль 100*0.9-60 = 30
    assert "1 день: 1 шт · выручка 100 ₽ · прибыль <b>30 ₽</b>" in block, block
    # 7 дней: 2 шт, 220 ₽, 198-120 = 78
    assert "7 дней: 2 шт · выручка 220 ₽ · прибыль <b>78 ₽</b>" in block, block
    # 30 дней: 3 шт, 320 ₽, 288-180 = 108
    assert "30 дней: 3 шт · выручка 320 ₽ · прибыль <b>108 ₽</b>" in block, block
    b500 = text.split("🔑")[2]
    assert "1 день: 1 шт · выручка 400 ₽ · прибыль <b>60 ₽</b>" in b500, b500
    assert "Комиссия Playerok:</b> 10%" in text
    assert _num("12,5") == 12.5 and _num("45 ₽") == 45 and _num("-3") is None and _num("abc") is None
    print("OK")
asyncio.run(main())

# кнопки калькулятора не пересекаются с другими экранами (раньше «pf» перехватывал профиль)
import re, pathlib
root = pathlib.Path(__file__).resolve().parents[1] / "bot"
others = "".join(p.read_text() for p in root.rglob("*.py") if p.name != "stats.py")
assert not re.search(r'"pc[:"]', others), "префикс pc занят другим экраном"
print("OK")
