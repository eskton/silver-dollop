import asyncio, os, sys
from types import SimpleNamespace
os.environ["SECRET_KEY"]=__import__("cryptography.fernet",fromlist=["Fernet"]).Fernet.generate_key().decode()
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from bot.db import init_db, DeliveryItem
from bot.handlers.settings import render_lot_detail, render_autodelivery, _h, _mask
from bot.services import features as ft

async def main():
    sessions = await init_db("sqlite+aiosqlite:///:memory:")
    async with sessions() as s:
        s.add_all([
            DeliveryItem(seller_tg_id=1, item_name="100 РОБУКСОВ | ПРОМОКОД", item_key="100 робуксов | промокод", content="CODE-AAAA-BBBB"),
            DeliveryItem(seller_tg_id=1, item_name="100 РОБУКСОВ | ПРОМОКОД", item_key="100 робуксов | промокод", content="CODE-CCCC"),
            DeliveryItem(seller_tg_id=1, item_name="100 РОБУКСОВ | ПРОМОКОД", item_key="100 робуксов | промокод", content="USED", used_deal_id="d1"),
        ])
        await s.commit()
    seller = SimpleNamespace(tg_id=1, is_connected=True, playerok_username="e")
    text, kb = await render_autodelivery(sessions, seller, ft.FEATURE_BY_KEY["autodelivery"])
    key = "100 робуксов | промокод"; h = _h(key)
    assert any(f"ad:lot:{h}" == b.callback_data for r in kb.inline_keyboard for b in r)
    assert "осталось 2 из 3" in text
    dt, dkb = await render_lot_detail(sessions, 1, h)
    assert "Свободно:</b> 2" in dt and "Выдано:</b> 1" in dt
    assert "CODE-AAAA-BBBB" in dt
    btns = [b.callback_data for r in dkb.inline_keyboard for b in r]
    assert f"ad:addto:{h}" in btns and f"ad:clear:{h}" in btns
    rm = [b for b in btns if b.startswith("ad:rm:")]
    assert len(rm) == 2
    assert all(len(b.callback_data.encode())<=64 for r in dkb.inline_keyboard for b in r)
    # удаляем один
    async with sessions() as s:
        item_id = int(rm[0].split(":")[2])
        it = await s.get(DeliveryItem, item_id); await s.delete(it); await s.commit()
    dt2, _ = await render_lot_detail(sessions, 1, h)
    assert "Свободно:</b> 1" in dt2
    assert _mask("x"*50).endswith("xxxxxx") and len(_mask("x"*50)) < 50
    print("OK")
asyncio.run(main())
