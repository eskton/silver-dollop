import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)

dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(f"Привет, {message.from_user.first_name}! Я твой бот. /help — список команд.")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer("/start — приветствие\n/help — эта справка\nИли просто напиши мне что-нибудь.")


@dp.message(F.text)
async def echo(message: Message) -> None:
    await message.answer(message.text)


async def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Задай BOT_TOKEN в .env (получить у @BotFather)")
    bot = Bot(token=token)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
