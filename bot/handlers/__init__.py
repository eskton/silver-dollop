from aiogram import Router

from ..plugins import plugin_routers
from . import account, admin, auth, chat, logs, market, pricing, settings, start, stats
from .confirm import ConfirmDelete


def build_router() -> Router:
    # Сначала роутеры с состояниями (вход, ответ покупателю, настройки), затем общие.
    root = Router(name="root")
    root.callback_query.outer_middleware(ConfirmDelete())  # «Точно удалить?» для кнопок удаления
    root.include_routers(auth.router, chat.router, admin.router, logs.router, pricing.router, market.router,
                      *plugin_routers())
    root.include_routers(settings.router, start.router, account.router, stats.router)
    return root
