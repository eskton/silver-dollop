from aiogram import Router

from . import account, auth, chat, start


def build_router() -> Router:
    # Сначала роутеры с состояниями (вход, ответ покупателю), затем общие.
    root = Router(name="root")
    root.include_routers(auth.router, chat.router, start.router, account.router)
    return root
