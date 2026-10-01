from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint, func
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


class SellerSetting(Base):
    """Произвольные настройки продавца: флаги функций, интервалы, лимиты."""

    __tablename__ = "seller_settings"
    __table_args__ = (UniqueConstraint("seller_tg_id", "key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(String(255))


class Template(Base):
    """Тексты автосообщений. item_name заполнен — правило для конкретного лота."""

    __tablename__ = "templates"
    __table_args__ = (UniqueConstraint("seller_tg_id", "kind", "item_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    item_name: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text)


class AutoReply(Base):
    """Автоответчик: ключевое слово в сообщении покупателя → ответ."""

    __tablename__ = "auto_replies"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    keyword: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)


class DeliveryItem(Base):
    """Единица товара для автовыдачи, привязана к лоту по названию."""

    __tablename__ = "delivery_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    item_name: Mapped[str] = mapped_column(String(255))
    # Название в нижнем регистре: lower() в SQLite не понимает кириллицу.
    item_key: Mapped[str] = mapped_column(String(255), index=True)
    content: Mapped[str] = mapped_column(Text)
    used_deal_id: Mapped[str | None] = mapped_column(String(128))
    used_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class DealState(Base):
    """Что бот уже сделал по сделке: нужно для переходов статусов и напоминаний."""

    __tablename__ = "deal_states"
    __table_args__ = (UniqueConstraint("seller_tg_id", "deal_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    deal_id: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[str | None] = mapped_column(String(128))
    item_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    confirm_reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    review_reminded: Mapped[bool] = mapped_column(Boolean, default=False)
    review_thanked: Mapped[bool] = mapped_column(Boolean, default=False)


class ChatState(Base):
    """Для текста при игноре и «не в сети»: когда покупатель писал, отвечали ли."""

    __tablename__ = "chat_states"
    __table_args__ = (UniqueConstraint("seller_tg_id", "chat_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[str] = mapped_column(String(128))
    last_buyer_message_id: Mapped[str | None] = mapped_column(String(128))
    last_buyer_message_at: Mapped[datetime | None] = mapped_column(DateTime)
    ignore_sent_for: Mapped[str | None] = mapped_column(String(128))
    offline_sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class ActionLog(Base):
    """Счётчики действий за сутки (лимит автоподнятия и т.п.)."""

    __tablename__ = "action_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    target: Mapped[str] = mapped_column(String(128), default="")
    cost: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


SessionFactory = async_sessionmaker[AsyncSession]


async def init_db(db_url: str) -> SessionFactory:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, expire_on_commit=False)
