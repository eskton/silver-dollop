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
    from .stars.handlers import router as stars_router

    return [stars_router]
