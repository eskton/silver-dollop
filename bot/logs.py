"""Логи в файл на volume (их присылает команда /logs) и пометка строк продавцом.

Строки, относящиеся к продавцу, помечаются `[tg=<id>]` (через `tag()`), чтобы
продавец мог получить только свои строки, а владелец бота — весь файл.
В логи не пишем ни токены, ни seed-фразы, ни сами выдаваемые товары.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_log_path: Path | None = None


def setup_file_logging(data_dir: str | None = None) -> Path:
    """Добавляет к корневому логгеру файл с ротацией (до ~4 МБ суммарно)."""
    global _log_path
    base = Path(data_dir or os.getenv("DATA_DIR", "/app/data")) / "logs"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "bot.log"
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path for h in root.handlers):
        handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(FORMAT))
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
    _log_path = path
    return path


def log_path() -> Path | None:
    return _log_path


def tag(tg_id: int | None) -> str:
    return f"[tg={tg_id}]"


def read_tail(max_bytes: int = 1_500_000, only_tg: int | None = None) -> str:
    """Последние строки логов (текущий файл + предыдущий), по желанию только одного продавца."""
    if _log_path is None:
        return ""
    files = [p for p in (_log_path.with_name("bot.log.1"), _log_path) if p.exists()]
    text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in files)
    if only_tg is not None:
        marker = tag(only_tg)
        text = "".join(line for line in text.splitlines(keepends=True) if marker in line)
    return text[-max_bytes:]
