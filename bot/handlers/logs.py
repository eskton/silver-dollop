"""/logs — присылает файл логов: владельцу весь, продавцу только его строки."""

from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, Message

from ..logs import log_path, read_tail
from ..plugins.access import is_admin

router = Router(name="logs")


@router.message(Command("logs"))
async def send_logs(message: Message) -> None:
    if log_path() is None:
        await message.answer("Логи пока не пишутся в файл. Перезапустите бота и попробуйте снова.")
        return
    admin = is_admin(message.from_user.id)
    text = read_tail(only_tg=None if admin else message.from_user.id)
    if not text.strip():
        await message.answer("В логах пока нет записей по вашему аккаунту.")
        return
    name = f"logs-{datetime.utcnow():%Y%m%d-%H%M}.txt"
    caption = (
        "📄 Логи бота (все записи)." if admin else "📄 Логи по вашему аккаунту."
    ) + " Если что-то не работает — пришлите этот файл разработчику."
    await message.answer_document(BufferedInputFile(text.encode("utf-8"), filename=name), caption=caption)
