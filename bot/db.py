from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, String, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Seller(Base):
    """Продавец = пользователь Telegram, подключивший свой аккаунт Playerok."""

    __tablename__ = "sellers"

    tg_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    tg_username: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(255))
    playerok_id: Mapped[str | None] = mapped_column(String(64))
    playerok_username: Mapped[str | None] = mapped_column(String(64))
    token_enc: Mapped[str | None] = mapped_column(String(1024))
    notify_deals: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_messages: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def is_connected(self) -> bool:
        return self.token_enc is not None


class SeenEvent(Base):
    """Уже отправленные уведомления, чтобы не слать одно и то же дважды."""

    __tablename__ = "seen_events"
    __table_args__ = (UniqueConstraint("seller_tg_id", "kind", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # deal | message
    external_id: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


SessionFactory = async_sessionmaker[AsyncSession]


async def init_db(db_url: str) -> SessionFactory:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)
