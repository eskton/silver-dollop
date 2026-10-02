"""Реестр функций автоматизации: описания, шаблоны текстов, числовые параметры.

Все тексты и настройки хранятся по продавцу в таблицах Template и SellerSetting.
Здесь только описание того, что бывает, и значения по умолчанию.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import Seller, SellerSetting, Template
from ..playerok import Deal


@dataclass(frozen=True)
class Param:
    key: str
    label: str
    unit: str
    default: int
    minimum: int = 0
    maximum: int = 100000


@dataclass(frozen=True)
class TemplateKind:
    kind: str
    label: str
    default: str
    per_item: bool = False  # можно задавать отдельный текст для лота


@dataclass(frozen=True)
class Feature:
    key: str
    title: str
    description: str
    templates: tuple[TemplateKind, ...] = ()
    params: tuple[Param, ...] = ()
    toggles: tuple[tuple[str, str, bool], ...] = ()  # (ключ настройки, подпись, по умолчанию)
    default_enabled: bool = False
    note: str = ""
    special: str = ""  # отдельный экран: autodelivery | autoresponder


VARIABLES_HELP = (
    "<b>Переменные:</b>\n"
    "<code>{Имя_Клиента}</code> — ник покупателя\n"
    "<code>{Ссылка}</code> — ссылка на лот\n"
    "<code>{Ссылка_Заказа}</code> — ссылка на заказ\n"
    "<code>{Ссылка_Чата}</code> — ссылка на чат\n"
    "<code>{Название_Лота}</code> — название лота\n"
    "<code>{Цена}</code> — цена лота\n"
    "<code>{Аккаунт}</code> — аккаунт продавца"
)

FEATURES: tuple[Feature, ...] = (
    Feature(
        key="greeting",
        title="👋 Приветствие",
        description="Автоматическое приветственное сообщение каждому новому покупателю в чате сделки.",
        templates=(
            TemplateKind(
                "greeting",
                "приветствие",
                "Привет, {Имя_Клиента}! Спасибо за покупку, скоро я выполню ваш заказ.",
                per_item=True,
            ),
        ),
        default_enabled=True,
    ),
    Feature(
        key="autoconfirm",
        title="🤝 Автоподтверждение",
        description="Бот сам отмечает заказ выполненным после выдачи товара.",
        templates=(
            TemplateKind(
                "autoconfirm_msg",
                "сообщение покупателю",
                "Заказ выполнен. Пожалуйста, подтвердите получение: {Ссылка_Заказа}",
            ),
        ),
        params=(Param("autoconfirm_delay", "Задержка", "мин", 0, 0, 1440),),
        toggles=(
            ("autoconfirm_only_delivered", "Подтверждать только после автовыдачи", True),
            ("autoconfirm_msg_enabled", "Сообщение покупателю", True),
        ),
        note="⚠️ Если «только после автовыдачи» выключено, бот подтвердит любой оплаченный заказ — "
        "включай это только для лотов, где выдавать ничего не нужно.",
    ),
    Feature(
        key="bump",
        title="🚀 Автоподнятие лотов",
        description="Периодическое поднятие активных лотов в топ выдачи. На Playerok поднятие "
        "платное, поэтому есть лимит в рублях на сутки.",
        params=(
            Param("bump_interval_hours", "Интервал поднятия", "ч", 168, 1, 8760),
            Param("bump_daily_limit", "Лимит на сутки", "₽", 1, 0, 100000),
            Param("bump_cost", "Стоимость одного поднятия", "₽", 1, 0, 10000),
        ),
    ),
    Feature(
        key="relist",
        title="🔁 Автовыставление лотов",
        description="После продажи товара бот выставляет такой же лот заново: все проданные "
        "лоты или только выбранные по ключевому слову в названии или ссылке. Проверка раз в "
        "~10 минут, поэтому возможна задержка.",
        params=(Param("relist_interval_hours", "Интервал", "ч", 1, 0, 720),),
        toggles=(
            ("relist_all", "Восстанавливать все лоты", True),
            ("relist_paid_allowed", "Платное восстановление", False),
        ),
        special="relist",
        note="Если платное восстановление выключено, бот выставляет только лоты с бесплатным "
        "статусом размещения.",
    ),
    Feature(
        key="autodelivery",
        title="📦 Автовыдача",
        description="После оплаты бот сам отправляет покупателю товар из запаса для этого лота.",
        templates=(
            TemplateKind(
                "delivery_msg",
                "текст перед товаром",
                "Спасибо за покупку, {Имя_Клиента}! Ваш товар:",
            ),
        ),
        special="autodelivery",
        default_enabled=True,
    ),
    Feature(
        key="autoresponder",
        title="🤖 Автоответчик",
        description="Ответы на сообщения покупателей по ключевым словам.",
        special="autoresponder",
        default_enabled=True,
    ),
    Feature(
        key="confirm_reminder",
        title="⏰ Напоминание о подтверждении",
        description="Если покупатель не подтвердил заказ после выдачи, бот напомнит ему: "
        "один раз или циклично, пока не подтвердит.",
        templates=(
            TemplateKind(
                "confirm_reminder",
                "напоминание",
                "Здравствуйте! Если вы уже получили товар и всё в порядке, пожалуйста, "
                "подтвердите получение заказа: {Ссылка_Заказа}",
            ),
        ),
        params=(
            Param("confirm_reminder_minutes", "Первая задержка", "мин", 60, 1, 43200),
            Param("confirm_reminder_repeat_minutes", "Повтор каждые", "мин", 180, 5, 43200),
        ),
        toggles=(("confirm_reminder_cyclic", "Режим: цикличный", True),),
    ),
    Feature(
        key="review_reminder",
        title="⭐ Напоминание об отзыве",
        description="Через заданное время после подтверждения заказа бот попросит оставить отзыв, "
        "если его ещё нет.",
        templates=(
            TemplateKind(
                "review_reminder",
                "просьба об отзыве",
                "{Имя_Клиента}, буду благодарен за отзыв о покупке!",
            ),
        ),
        params=(Param("review_reminder_hours", "Через", "ч", 1, 1, 720),),
    ),
    Feature(
        key="offline",
        title="💤 Текст, если не в сети",
        description="Когда ты включил режим «не в сети», бот отвечает этим текстом на первое "
        "сообщение покупателя (не чаще раза в несколько часов на чат).",
        templates=(
            TemplateKind(
                "offline",
                "текст",
                "Сейчас я не в сети, отвечу как только появлюсь.",
            ),
        ),
        params=(Param("offline_repeat_hours", "Не чаще раза в", "ч", 6, 1, 168),),
        toggles=(("offline_mode", "Режим «не в сети»", False),),
    ),
    Feature(
        key="ignore",
        title="💬 Текст при игноре",
        description="Если на сообщение покупателя никто не ответил за заданное время, "
        "бот отправит этот текст.",
        templates=(
            TemplateKind(
                "ignore",
                "текст",
                "Извините за задержку, скоро отвечу!",
            ),
        ),
        params=(Param("ignore_minutes", "Через", "мин", 10, 1, 1440),),
    ),
    Feature(
        key="after_review",
        title="💬 Тексты после отзывов",
        description="Ответ покупателю в чат после того, как он оставил отзыв.",
        templates=(
            TemplateKind("review_good", "после хорошего отзыва (4–5)", "Спасибо за отзыв! 🙏"),
            TemplateKind(
                "review_bad",
                "после плохого отзыва (1–3)",
                "Жаль, что что-то пошло не так. Напишите, что случилось — постараюсь помочь.",
            ),
        ),
    ),
    Feature(
        key="problem",
        title="💬 Текст при проблеме",
        description="Если покупатель открыл спор или пожаловался на заказ, бот отправит этот текст "
        "и уведомит тебя.",
        templates=(
            TemplateKind(
                "problem",
                "текст",
                "Вижу, что возникла проблема. Уже разбираюсь, скоро отвечу.",
            ),
        ),
        default_enabled=True,
    ),
    Feature(
        key="after_seller_confirm",
        title="💬 Текст после вашего подтверждения",
        description="Отправляется покупателю, когда ты (или бот) отметил заказ выполненным.",
        templates=(
            TemplateKind(
                "after_seller_confirm",
                "текст",
                "Заказ выдан! Пожалуйста, подтвердите получение: {Ссылка_Заказа}",
            ),
        ),
    ),
    Feature(
        key="after_buyer_confirm",
        title="💬 Текст после подтверждения клиентом",
        description="Отправляется, когда покупатель подтвердил получение.",
        templates=(
            TemplateKind(
                "after_buyer_confirm",
                "текст",
                "Спасибо за покупку, {Имя_Клиента}! Буду рад отзыву 😊",
            ),
        ),
    ),
)

FEATURE_BY_KEY: dict[str, Feature] = {f.key: f for f in FEATURES}
TEMPLATE_KINDS: dict[str, TemplateKind] = {t.kind: t for f in FEATURES for t in f.templates}
PARAMS: dict[str, Param] = {p.key: p for f in FEATURES for p in f.params}


# ----- настройки -----


async def get_setting(session: AsyncSession, tg_id: int, key: str, default: str) -> str:
    row = await session.scalar(
        select(SellerSetting).where(SellerSetting.seller_tg_id == tg_id, SellerSetting.key == key)
    )
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, tg_id: int, key: str, value: str) -> None:
    row = await session.scalar(
        select(SellerSetting).where(SellerSetting.seller_tg_id == tg_id, SellerSetting.key == key)
    )
    if row is None:
        session.add(SellerSetting(seller_tg_id=tg_id, key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def is_enabled(session: AsyncSession, tg_id: int, feature: Feature) -> bool:
    default = "1" if feature.default_enabled else "0"
    return await get_setting(session, tg_id, f"{feature.key}_enabled", default) == "1"


async def get_flag(session: AsyncSession, tg_id: int, key: str, default: bool = False) -> bool:
    return await get_setting(session, tg_id, key, "1" if default else "0") == "1"


async def get_param(session: AsyncSession, tg_id: int, key: str) -> int:
    param = PARAMS[key]
    raw = await get_setting(session, tg_id, key, str(param.default))
    try:
        return int(raw)
    except ValueError:
        return param.default


DEFAULT_TZ = 3  # МСК


async def get_tz(session: AsyncSession, tg_id: int) -> int:
    """Смещение часового пояса продавца от UTC в часах."""
    try:
        return int(await get_setting(session, tg_id, "tz", str(DEFAULT_TZ)))
    except ValueError:
        return DEFAULT_TZ


async def load_all_settings(session: AsyncSession, tg_id: int) -> dict[str, str]:
    rows = await session.scalars(select(SellerSetting).where(SellerSetting.seller_tg_id == tg_id))
    return {r.key: r.value for r in rows}


# ----- шаблоны -----


async def get_template(
    session: AsyncSession, tg_id: int, kind: str, item_name: str = ""
) -> str:
    """Текст шаблона: сначала правило для лота, потом общий, потом дефолт."""
    rows = list(
        await session.scalars(
            select(Template).where(Template.seller_tg_id == tg_id, Template.kind == kind)
        )
    )
    if item_name:
        for r in rows:
            if r.item_name and r.item_name.lower() == item_name.lower():
                return r.text
    for r in rows:
        if not r.item_name:
            return r.text
    return TEMPLATE_KINDS[kind].default


async def set_template(
    session: AsyncSession, tg_id: int, kind: str, text: str, item_name: str = ""
) -> None:
    row = await session.scalar(
        select(Template).where(
            Template.seller_tg_id == tg_id, Template.kind == kind, Template.item_name == item_name
        )
    )
    if row is None:
        session.add(Template(seller_tg_id=tg_id, kind=kind, item_name=item_name, text=text))
    else:
        row.text = text
    await session.commit()


async def item_templates(session: AsyncSession, tg_id: int, kind: str) -> list[Template]:
    rows = await session.scalars(
        select(Template).where(
            Template.seller_tg_id == tg_id, Template.kind == kind, Template.item_name != ""
        )
    )
    return list(rows)


def render(text: str, seller: Seller, deal: Deal | None = None, buyer: str = "") -> str:
    """Подстановка переменных вида {Имя_Клиента}."""
    values = {
        "Имя_Клиента": (deal.buyer_username if deal else buyer) or "покупатель",
        "Ссылка": deal.item_url if deal else "",
        "Ссылка_Заказа": deal.deal_url if deal else "",
        "Ссылка_Чата": deal.chat_url if deal else "",
        "Название_Лота": deal.item_name if deal else "",
        "Цена": f"{deal.price:g} ₽" if deal and isinstance(deal.price, (int, float)) else "",
        "Аккаунт": seller.playerok_username or "",
    }
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def quote(text: str) -> str:
    return f"<blockquote>{html.escape(text)}</blockquote>"
