from aiogram import Router

from . import account, auth, chat, settings, start, stars, stats


def build_router() -> Router:
    # Сначала роутеры с состояниями (вход, ответ покупателю, настройки), затем общие.
    root = Router(name="root")
    root.include_routers(
        auth.router, chat.router, stars.router, settings.router, start.router, account.router, stats.router
    )
    return root
