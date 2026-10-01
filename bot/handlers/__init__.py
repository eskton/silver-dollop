from aiogram import Router

from . import account, auth, chat, settings, start


def build_router() -> Router:
    # Сначала роутеры с состояниями (вход, ответ покупателю, настройки), затем общие.
    root = Router(name="root")
    root.include_routers(auth.router, chat.router, settings.router, start.router, account.router)
    return root
