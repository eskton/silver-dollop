from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    inspect,
    text,
)
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


class RelistRule(Base):
    """Правило отбора лотов для автовыставления: слово в названии или ссылка."""

    __tablename__ = "relist_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    pattern: Mapped[str] = mapped_column(String(255))


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
    # Для аналитики: цена, покупатель, когда создан заказ на Playerok, оценка.
    price: Mapped[float | None] = mapped_column(Float)
    buyer: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_rating: Mapped[int | None] = mapped_column(Integer)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    autoconfirm_failed: Mapped[bool | None] = mapped_column(Boolean, default=False)
    confirm_reminded_at: Mapped[datetime | None] = mapped_column(DateTime)
    confirm_reminders: Mapped[int] = mapped_column(Integer, default=0)
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


class StarsRule(Base):
    """Правило: слово в названии лота → сколько звёзд выдавать."""

    __tablename__ = "stars_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    pattern: Mapped[str] = mapped_column(String(255))
    stars: Mapped[int] = mapped_column(Integer)


class StarsOrder(Base):
    """Заказ звёзд: от оплаты на Playerok до покупки на Fragment."""

    __tablename__ = "stars_orders"
    __table_args__ = (UniqueConstraint("seller_tg_id", "deal_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    deal_id: Mapped[str] = mapped_column(String(128))
    chat_id: Mapped[str | None] = mapped_column(String(128), index=True)
    item_name: Mapped[str] = mapped_column(String(255), default="")
    buyer: Mapped[str | None] = mapped_column(String(64))
    stars: Mapped[int] = mapped_column(Integer)
    # awaiting_username → processing → done | failed
    status: Mapped[str] = mapped_column(String(32), default="awaiting_username")
    username: Mapped[str | None] = mapped_column(String(64))
    recipient_name: Mapped[str | None] = mapped_column(String(255))
    fragment_req_id: Mapped[str | None] = mapped_column(String(128))
    tx_hash: Mapped[str | None] = mapped_column(String(128))
    cost_usdt: Mapped[float | None] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime)


class PluginAccess(Base):
    """Кому выдан платный плагин и до какого срока (NULL — бессрочно)."""

    __tablename__ = "plugin_access"
    __table_args__ = (UniqueConstraint("seller_tg_id", "plugin"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    seller_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    plugin: Mapped[str] = mapped_column(String(32))
    granted_by: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


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


def _add_missing_columns(sync_conn) -> None:
    """Мини-миграция: create_all не добавляет новые колонки в уже созданные
    таблицы, поэтому досоздаём их сами (все новые колонки допускают NULL)."""
    insp = inspect(sync_conn)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing:
                continue
            col_type = col.type.compile(dialect=sync_conn.dialect)
            sync_conn.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col_type}"))


async def init_db(db_url: str) -> SessionFactory:
    engine = create_async_engine(db_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
    return async_sessionmaker(engine, expire_on_commit=False)
