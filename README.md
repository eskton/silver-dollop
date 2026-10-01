# Бот-помощник продавца Playerok

Telegram-бот в духе FunPay Cardinal, но для [playerok.com](https://playerok.com) и сразу
для многих продавцов: каждый подключает свой аккаунт и получает собственные уведомления.

**Что умеет сейчас**
- вход в Playerok по почте и коду из письма, без cookie и паролей;
- уведомления о новых заказах и сообщениях покупателей;
- ответ покупателю прямо из Telegram (кнопка под уведомлением);
- настройки: какие уведомления присылать, проверка сессии, выход.

**В планах:** авто-ответы по шаблонам, авто-выдача товара после оплаты, статистика продаж.

## Запуск

1. Создай бота у [@BotFather](https://t.me/BotFather) (`/newbot`) и скопируй токен.
2. Сгенерируй ключ шифрования:
   `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
3. `cp .env.example .env` и заполни `BOT_TOKEN` и `SECRET_KEY`.
4. `pip install -r requirements.txt`
5. `python -m bot.main`

### Docker / хостинг (Railway, Fly.io, любой VPS)

В репозитории есть `Dockerfile`. Задай переменные окружения `BOT_TOKEN` и `SECRET_KEY`
в настройках сервиса и подключи volume к `/app/data`, иначе база SQLite пропадёт при
перезапуске. Для Railway: New Project → Deploy from GitHub → выбрать репозиторий →
Variables → добавить переменные → в Settings добавить Volume с mount path `/app/data`.

## Структура

```
bot/
  main.py           точка входа: бот + фоновый поллер
  config.py         настройки из .env
  db.py             модели SQLAlchemy (продавцы, увиденные события)
  crypto.py         шифрование токенов Playerok (Fernet)
  keyboards.py      кнопки
  handlers/         обработчики Telegram: start, auth (вход), account, chat (ответы)
  services/
    poller.py       опрос Playerok → уведомления
    sellers.py      работа с продавцами в базе
  playerok/
    client.py       HTTP-клиент
    queries.py      GraphQL-запросы к сайту — единственное место, где они описаны
```

## Важно про Playerok

У площадки нет публичного API: бот повторяет GraphQL-запросы, которые делает сам сайт.
Запросы лежат в `bot/playerok/queries.py`. Если Playerok изменит их, поправить нужно только
этот файл: открой playerok.com → DevTools → Network → фильтр `graphql`, найди операцию
с тем же названием и сверь поля.

Токены сессий продавцов хранятся в базе в зашифрованном виде. Потеря или смена `SECRET_KEY`
означает, что всем продавцам придётся войти заново.
