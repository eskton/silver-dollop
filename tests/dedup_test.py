import asyncio, os, sys
from types import SimpleNamespace
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
from sqlalchemy import select, func
from bot.db import init_db, DeliveryItem
from bot.handlers import settings as S
from aiogram.fsm.context import FSMContext

class Msg:
    def __init__(self, text): self.text=text; self.from_user=SimpleNamespace(id=1,username="u"); self.out=[]
    async def answer(self, t, **k): self.out.append(t)
class State:
    def __init__(self, d): self._d=d
    async def get_data(self): return self._d
    async def clear(self): pass
    async def set_state(self,*a): pass
    async def update_data(self,**k): self._d.update(k)

async def main():
    ss=await init_db("sqlite+aiosqlite:///:memory:")
    S_sessions = ss
    # первый раз: 3 кода
    m=Msg("E4Y8Y\nNFGR7\nNEYZ5")
    await S.stock_items(m, State({"lot":"100 РОБУКСОВ","feature":"autodelivery"}), ss)
    async with ss() as s:
        assert await s.scalar(select(func.count(DeliveryItem.id)))==3
        # пометим один выданным
        it=await s.scalar(select(DeliveryItem).where(DeliveryItem.content=="E4Y8Y"))
        it.used_deal_id="d1"; await s.commit()
    # повторно те же 3 (как после «отката») — ничего не добавится, выданный не вернётся свободным
    m2=Msg("E4Y8Y\nNFGR7\nNEYZ5\nNEWCODE")
    await S.stock_items(m2, State({"lot":"100 РОБУКСОВ","feature":"autodelivery"}), ss)
    async with ss() as s:
        total=await s.scalar(select(func.count(DeliveryItem.id)))
        free=await s.scalar(select(func.count(DeliveryItem.id)).where(DeliveryItem.used_deal_id.is_(None)))
        assert total==4, total          # только NEWCODE добавился
        assert free==3, free            # E4Y8Y остался выданным, не продублировался
    note=[o for o in m2.out if "Пропущено" in o][0]; assert "Пропущено дублей" in note and "3" in note
    print("OK")
asyncio.run(main())
