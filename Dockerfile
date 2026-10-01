FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot

# База SQLite лежит в /app/data — подключи сюда volume, чтобы она переживала перезапуски.
VOLUME ["/app/data"]

CMD ["python", "-m", "bot.main"]
