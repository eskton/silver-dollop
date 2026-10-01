from aiogram.types import User
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import Seller, SessionFactory


async def get_or_create_seller(session: AsyncSession, user: User) -> Seller:
    seller = await session.get(Seller, user.id)
    if seller is None:
        seller = Seller(tg_id=user.id, tg_username=user.username)
        session.add(seller)
        await session.commit()
    elif seller.tg_username != user.username:
        seller.tg_username = user.username
        await session.commit()
    return seller


async def disconnect_seller(sessions: SessionFactory, tg_id: int) -> None:
    """Удаляет токен Playerok; почта и имя остаются для повторного входа."""
    async with sessions() as session:
        seller = await session.get(Seller, tg_id)
        if seller is not None and seller.token_enc is not None:
            seller.token_enc = None
            await session.commit()
