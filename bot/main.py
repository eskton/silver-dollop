import asyncio
import logging
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from .config import load_settings
from .crypto import TokenCipher
from .db import init_db
from .handlers import build_router
from .services.poller import run_poller

COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="login", description="Войти в Playerok"),
    BotCommand(command="account", description="Мой аккаунт"),
    BotCommand(command="notify", description="Настройки уведомлений"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить действие"),
]


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = load_settings()
    cipher = TokenCipher(settings.secret_key)
    Path("data").mkdir(exist_ok=True)
    sessions = await init_db(settings.db_url)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(build_router())
    # Эти объекты aiogram подставляет в обработчики по имени аргумента.
    dp["settings"] = settings
    dp["sessions"] = sessions
    dp["cipher"] = cipher

    await bot.set_my_commands(COMMANDS)
    poller = asyncio.create_task(run_poller(bot, sessions, cipher, settings))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        poller.cancel()


if __name__ == "__main__":
    asyncio.run(main())
