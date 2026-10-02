import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    secret_key: str
    db_url: str
    poll_interval: int
    admin_ids: frozenset[int]


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    secret_key = os.getenv("SECRET_KEY", "").strip()
    if not bot_token:
        raise SystemExit("Задай BOT_TOKEN в .env (получить у @BotFather)")
    if not secret_key:
        raise SystemExit(
            "Задай SECRET_KEY в .env. Сгенерировать: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    admin_ids = frozenset(
        int(x) for x in os.getenv("ADMIN_IDS", "").replace(",", " ").split() if x.strip()
    )
    # База по умолчанию — абсолютный путь в DATA_DIR (на Railway это volume).
    # Абсолютный путь, чтобы файл не зависел от рабочей директории процесса.
    data_dir = os.getenv("DATA_DIR", "/app/data")
    default_db = f"sqlite+aiosqlite:///{os.path.join(data_dir, 'bot.db')}"
    return Settings(
        bot_token=bot_token,
        secret_key=secret_key,
        db_url=os.getenv("DB_URL", default_db),
        poll_interval=int(os.getenv("POLL_INTERVAL", "30")),
        admin_ids=admin_ids,
    )
