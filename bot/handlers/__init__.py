from aiogram import Router

from ..plugins import plugin_routers
from . import account, admin, auth, chat, logs, settings, start, stats


def build_router() -> Router:
    # Сначала роутеры с состояниями (вход, ответ покупателю, настройки), затем общие.
    root = Router(name="root")
    root.include_routers(auth.router, chat.router, admin.router, logs.router, *plugin_routers())
    root.include_routers(settings.router, start.router, account.router, stats.router)
    return root
