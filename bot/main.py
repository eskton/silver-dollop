import asyncio
import logging
import sys
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
    BotCommand(command="settings", description="Автоматизация"),
    BotCommand(command="stats", description="Аналитика продаж"),
    BotCommand(command="profile", description="Профиль: уведомления, заказы, клиенты"),
    BotCommand(command="notify", description="Настройки уведомлений"),
    BotCommand(command="logs", description="Логи бота файлом"),
    BotCommand(command="help", description="Помощь"),
    BotCommand(command="cancel", description="Отменить действие"),
]


async def main() -> None:
    # В stdout: Railway помечает всё из stderr как Error, даже обычные INFO.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    settings = load_settings()
    try:
        from .logs import setup_file_logging

        logging.info("Логи пишутся в %s", setup_file_logging())
    except OSError as e:  # нет доступа к диску — работаем без файла
        logging.warning("Не удалось открыть файл логов: %s", e)
    cipher = TokenCipher(settings.secret_key)
    # Логируем, где реально лежит база и сохранилась ли она — так по логам после
    # перезапуска видно, работает ли том (volume) и не обнулилась ли база.
    db_path = settings.db_url.split("///", 1)[-1] if settings.db_url.startswith("sqlite") else ""
    if db_path:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        existed = Path(db_path).exists()
        logging.info("База: %s (файл %s)", db_path, "уже был" if existed else "создаётся заново")
    sessions = await init_db(settings.db_url)
    if db_path:
        from sqlalchemy import func, select

        from .db import DeliveryItem, Seller

        async with sessions() as s:
            sellers = await s.scalar(select(func.count(Seller.tg_id)))
            items = await s.scalar(select(func.count(DeliveryItem.id)))
            used = await s.scalar(
                select(func.count(DeliveryItem.id)).where(DeliveryItem.used_deal_id.is_not(None))
            )
        logging.info("В базе: продавцов %s, товаров автовыдачи %s (выдано %s)", sellers, items, used)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
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
