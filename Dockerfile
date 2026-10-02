FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot

# База SQLite лежит в /app/data. Чтобы она переживала перезапуски, подключи сюда
# volume хостинга (на Railway: Volume с mount path /app/data). Инструкцию VOLUME
# не используем: Railway её не принимает.
RUN mkdir -p /app/data

CMD ["python", "-m", "bot.main"]
