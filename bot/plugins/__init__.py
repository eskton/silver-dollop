"""Плагины бота. Каждый плагин — пакет в этой папке со своим роутером и логикой.

Платные плагины перечислены в PAID_PLUGINS: доступ к ним выдаёт администратор
(см. access.py и команды /grant, /revoke, /access).
"""

from __future__ import annotations

from aiogram import Router

# ключ плагина → заголовок (для админских команд и экрана с замком)
PAID_PLUGINS: dict[str, str] = {
    "stars": "⭐ Звёзды через Fragment",
}


def plugin_routers() -> list[Router]:
    # Gift Card (FazerCards) — закрытый: только для администраторов, через /giftcard;
    # не выдаётся через /grant, т.к. покупки идут с баланса владельца.
    from .giftcard.handlers import router as giftcard_router
    from .stars.handlers import router as stars_router

    return [stars_router, giftcard_router]
