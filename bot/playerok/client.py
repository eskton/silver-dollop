"""Асинхронный клиент Playerok поверх его GraphQL-эндпоинта.

Сессия Playerok живёт в cookie `token`. Клиент либо получает её при входе
по почте (`request_email_code` → `confirm_email_code`), либо создаётся
с уже сохранённым токеном продавца.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import queries as q
from .errors import AuthRequired, PlayerokError

log = logging.getLogger(__name__)

BASE_URL = "https://playerok.com"
# Статусы сделок, по которым продавцу надо что-то сделать.
ACTIVE_SALE_STATUSES = ("PAID", "SENT")


def _get(d: dict[str, Any] | None, *path: str, default: Any = None) -> Any:
    cur: Any = d or {}
    for key in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return default if cur is None else cur


@dataclass(frozen=True)
class Viewer:
    id: str
    username: str
    email: str | None
    balance: float | None

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Viewer":
        return cls(
            id=str(_get(raw, "id", default="")),
            username=str(_get(raw, "username", default="")),
            email=_get(raw, "email"),
            balance=_get(raw, "balance", "available"),
        )


@dataclass(frozen=True)
class Deal:
    id: str
    status: str
    item_id: str
    item_slug: str
    item_name: str
    price: float | None
    buyer_id: str
    buyer_username: str
    chat_id: str | None
    created_at: str
    review_rating: int | None = None
    review_text: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Deal":
        rating = _get(raw, "review", "rating")
        return cls(
            id=str(_get(raw, "id", default="")),
            status=str(_get(raw, "status", default="")),
            item_id=str(_get(raw, "item", "id", default="")),
            item_slug=str(_get(raw, "item", "slug", default="")),
            item_name=str(_get(raw, "item", "name", default="товар")),
            price=_get(raw, "item", "price"),
            buyer_id=str(_get(raw, "user", "id", default="")),
            buyer_username=str(_get(raw, "user", "username", default="покупатель")),
            chat_id=_get(raw, "chat", "id"),
            created_at=str(_get(raw, "createdAt", default="")),
            review_rating=int(rating) if isinstance(rating, (int, float)) else None,
            review_text=str(_get(raw, "review", "text", default="") or ""),
        )

    @property
    def item_url(self) -> str:
        return f"{BASE_URL}/products/{self.item_slug or self.item_id}"

    @property
    def deal_url(self) -> str:
        return f"{BASE_URL}/deal/{self.id}"

    @property
    def chat_url(self) -> str:
        return f"{BASE_URL}/chats/{self.chat_id}" if self.chat_id else f"{BASE_URL}/chats"


@dataclass(frozen=True)
class Item:
    id: str
    slug: str
    name: str
    price: float | None
    status: str
    position: int | None
    relist_price: float | None  # стоимость статуса размещения; 0/None — бесплатно
    raw_price: float | None = None  # цена продавца без комиссии — по ней считаются тарифы
    priority: str = ""  # DEFAULT / PREMIUM
    may_be_published: bool | None = None
    category_id: str = ""
    user_id: str = ""
    obtaining_type_id: str = ""  # способ получения (код, по нику, …)
    obtaining_type_name: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Item":
        pos = _get(raw, "priorityPosition")
        return cls(
            id=str(_get(raw, "id", default="")),
            slug=str(_get(raw, "slug", default="")),
            name=str(_get(raw, "name", default="лот")),
            price=_get(raw, "price"),
            status=str(_get(raw, "status", default="")),
            position=int(pos) if isinstance(pos, (int, float)) else None,
            relist_price=_get(raw, "priorityStatus", "price"),
            raw_price=_get(raw, "rawPrice"),
            priority=str(_get(raw, "priority", default="") or ""),
            may_be_published=_get(raw, "mayBePublished"),
            category_id=str(_get(raw, "category", "id", default="") or ""),
            user_id=str(_get(raw, "user", "id", default="") or ""),
            obtaining_type_id=str(_get(raw, "obtainingType", "id", default="") or ""),
            obtaining_type_name=str(_get(raw, "obtainingType", "name", default="") or ""),
        )

    @property
    def url(self) -> str:
        return f"{BASE_URL}/products/{self.slug or self.id}"

    @property
    def is_paid_relist(self) -> bool:
        return isinstance(self.relist_price, (int, float)) and self.relist_price > 0


@dataclass(frozen=True)
class ChatPreview:
    id: str
    unread: int
    last_message_id: str | None
    last_text: str
    last_author_id: str | None
    last_author_username: str
    created_at: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "ChatPreview":
        return cls(
            id=str(_get(raw, "id", default="")),
            unread=int(_get(raw, "unreadMessagesCounter", default=0) or 0),
            last_message_id=_get(raw, "lastMessage", "id"),
            last_text=str(_get(raw, "lastMessage", "text", default="")),
            last_author_id=_get(raw, "lastMessage", "user", "id"),
            last_author_username=str(
                _get(raw, "lastMessage", "user", "username", default="покупатель")
            ),
            created_at=str(_get(raw, "lastMessage", "createdAt", default="")),
        )


@dataclass(frozen=True)
class RawResponse:
    status: int
    text: str
    token: str | None  # значение cookie `token` из Set-Cookie, если пришло


# Транспорт: (тело запроса, текущий токен) → ответ. Подменяется в тестах.
Transport = Callable[[dict[str, Any], "str | None"], Awaitable[RawResponse]]

# Схема Playerok нам точно не известна: если сервер отвечает «нет такого поля»,
# поле убирается из запроса, запрос повторяется, а исправленный текст кэшируется.
UNKNOWN_FIELD_RE = re.compile(r'Cannot query field "([A-Za-z_][A-Za-z0-9_]*)"')
# Variable "$filter" of type "ItemDealFilter" used in position expecting type "ItemDealFilter!"
VAR_TYPE_RE = re.compile(
    r'Variable "\$([A-Za-z_][A-Za-z0-9_]*)" of type "([^"]+)" used in position expecting type "([^"]+)"'
)


def fix_variable_type(query: str, name: str, old: str, new: str) -> str:
    """Меняет тип переменной в объявлении операции: `$name: old` → `$name: new`."""
    return re.sub(r"(\$" + re.escape(name) + r"\s*:\s*)" + re.escape(old) + r"(?![A-Za-z0-9_!\]])", r"\g<1>" + new, query, count=1)
MAX_FIELD_RETRIES = 12
PATCHED_QUERIES: dict[str, str] = {}
REMOVED_FIELDS: dict[str, list[str]] = {}


def strip_field(query: str, field: str) -> str:
    """Убирает из GraphQL-запроса все поля `field` вместе с их аргументами
    и вложенным блоком. Корневое поле операции не трогает."""
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(field) + r"(?![A-Za-z0-9_])")
    out = query
    pos = 0
    while True:
        m = pattern.search(out, pos)
        if not m:
            return out
        start, end = m.start(), m.end()
        # Пропускаем аргументы (...) и вложенный блок {...}
        i = end
        for opener, closer in (("(", ")"), ("{", "}")):
            j = i
            while j < len(out) and out[j] in " \t\r\n":
                j += 1
            if j < len(out) and out[j] == opener:
                depth = 0
                while j < len(out):
                    if out[j] == opener:
                        depth += 1
                    elif out[j] == closer:
                        depth -= 1
                        if depth == 0:
                            j += 1
                            break
                    j += 1
                i = j
        # Поле-корень (сразу после `query x(...) {`) или алиас/название операции — не трогаем
        before = out[:start].rstrip()
        if before.endswith(("query", "mutation")) or before.endswith("{") and before.count("{") == 1:
            pos = end
            continue
        out = out[:start] + out[i:]
        pos = start


# Сохранённые запросы сайта (GET + sha256 текста запроса). Сайт отдаёт эти данные
# только так — на произвольный текст запроса `items` отвечал «Access denied».
# Хеши — из PlayerokAPI (github.com/alleexxeeyy/PlayerokAPI, playerokapi/misc.py).
PERSISTED_QUERIES = {
    "items": "3f20c731f8f769a094ee3fa32e09f8e12250357e9a4f0ebb4e6988e7a0bb9260",
    "item": "1cdb4b335f6c119db77883451f41cef83fc449f79f021627f27b76ec49203487",
    "itemPriorityStatuses": "b922220c6f979537e1b99de6af8f5c13727daeff66727f679f07f986ce1c025a",
}

HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "apollo-require-preflight": "true",
}


# ----- ограничение частоты запросов -----
# Playerok отвечает «Too many requests» примерно после 15 запросов в минуту с аккаунта.
# Один общий лимит на токен для всех функций бота (опрос, лоты, цены, отчёты).
RPM_LIMIT = int(os.getenv("PLAYEROK_RPM", "12") or 12)
_RATE: dict[str, Any] = {}  # токен → (lock, deque времени запросов)
TOO_MANY_RE = re.compile(r"too many requests|rate limit", re.I)
RATE_RETRY_DELAYS = (10, 20)


async def _rate_wait(token: str | None, on_wait: Any = None) -> None:
    """Ждёт, если за последнюю минуту с этого токена уже RPM_LIMIT запросов."""
    import asyncio
    import collections
    import time

    if RPM_LIMIT <= 0:
        return
    key = token or ""
    if key not in _RATE:
        _RATE[key] = (asyncio.Lock(), collections.deque())
    lock, stamps = _RATE[key]
    async with lock:
        while True:
            now = time.monotonic()
            while stamps and now - stamps[0] >= 60:
                stamps.popleft()
            if len(stamps) < RPM_LIMIT:
                stamps.append(now)
                return
            pause = 60 - (now - stamps[0]) + 0.05
            if on_wait is not None and pause > 2:
                await on_wait(f"лимит Playerok — жду {pause:.0f} с")
            await asyncio.sleep(pause)


class PaidPlacementRefused(PlayerokError):
    """Бесплатного тарифа нет, а платное восстановление выключено продавцом."""


class PlayerokClient:
    def __init__(
        self, token: str | None = None, timeout: float = 20.0, transport: Transport | None = None
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self._session: Any = None
        # async-функция(текст) — сообщать о паузах из-за лимита (для экранов с прогрессом)
        self.on_wait: Any = None

    async def _curl_post(self, body: dict[str, Any], token: str | None) -> RawResponse:
        # curl_cffi повторяет TLS-отпечаток настоящего Chrome: без этого защита
        # Playerok от ботов отвечает 403 ещё до обработки запроса.
        from curl_cffi.requests import AsyncSession

        if self._session is None:
            # PLAYEROK_PROXY — прокси для запросов к Playerok (например, если сайт
            # не пускает IP хостинга): http://user:pass@host:port или socks5://...
            proxy = os.getenv("PLAYEROK_PROXY", "").strip() or None
            self._session = AsyncSession(impersonate="chrome", timeout=self._timeout, proxy=proxy)
        rest_path = body.get("rest")
        op = str(body.get("operationName") or "")
        headers = dict(HEADERS)
        if not rest_path:
            headers.update({
                "apollographql-client-name": "web",
                "x-apollo-operation-name": op,
                "x-gql-op": op,
                "x-gql-path": "/",
            })
        cookies = {"token": token} if token else None
        if body.get("persisted"):
            params = {
                "operationName": op,
                "variables": json.dumps(body.get("variables") or {}, ensure_ascii=False),
                "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": body["persisted"]}}),
            }
            resp = await self._session.get(BASE_URL + "/graphql", params=params, headers=headers, cookies=cookies)
        else:
            resp = await self._session.post(
                BASE_URL + (rest_path or "/graphql"),
                json=None if rest_path else body,
                headers=headers,
                cookies=cookies,
            )
        return RawResponse(resp.status_code, resp.text, self._extract_token(resp, body))

    def _extract_token(self, resp: Any, body: dict[str, Any]) -> str | None:
        """Ищет cookie `token` везде, куда curl_cffi может его положить."""
        found: str | None = None
        names: set[str] = set()
        for jar in (resp.cookies.jar, self._session.cookies.jar):
            for c in jar:
                names.add(c.name)
                if c.name == "token" and c.value:
                    found = c.value
        set_cookies = resp.headers.get_list("set-cookie") or []
        for raw in set_cookies:
            name, _, rest = raw.partition("=")
            names.add(name.strip())
            if name.strip() == "token" and not found:
                found = rest.split(";", 1)[0].strip() or None
        if body.get("operationName") == "checkEmailAuthCode":
            # Только имена, без значений: значения — это доступ к аккаунту.
            log.info(
                "checkEmailAuthCode: HTTP %s, cookies=%s, set-cookie=%d шт., токен %s",
                resp.status_code,
                sorted(names),
                len(set_cookies),
                "найден" if found else "НЕ найден",
            )
        return found

    @property
    def token(self) -> str | None:
        return self._token

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "PlayerokClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----- низкий уровень -----

    def _real_transport(self) -> bool:
        """Лимит частоты — только для настоящих запросов к сайту (в тестах транспорт подменён)."""
        return self._transport is None and type(self)._curl_post is _ORIGINAL_CURL_POST

    async def _gql(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        _retries: int = 0,
    ) -> dict[str, Any]:
        persisted = query.startswith("persisted:")
        if persisted:
            # Сохранённый запрос: шлём только хеш, текст сайт знает сам.
            body = {"operationName": operation, "variables": variables or {}, "persisted": query[10:]}
        else:
            # Если раньше из запроса уже выкидывали неизвестные поля — берём исправленный.
            query = PATCHED_QUERIES.get(operation, query)
            body = {"operationName": operation, "query": query, "variables": variables or {}}
        send = self._transport or self._curl_post
        if self._real_transport():
            await _rate_wait(self._token, self.on_wait)
        try:
            resp = await send(body, self._token)
        except Exception as e:  # сеть, таймаут, TLS
            raise PlayerokError(f"Playerok недоступен: {e.__class__.__name__}") from e

        try:
            payload = json.loads(resp.text)
        except ValueError:
            payload = None
        # «Too many requests»: запрос не выполнен — безопасно подождать и повторить.
        limited = resp.status == 429 or (
            isinstance(payload, dict)
            and any(TOO_MANY_RE.search(str((e or {}).get("message", ""))) for e in (payload.get("errors") or [])
                    if isinstance(e, dict))
        )
        if limited:
            attempt = getattr(self, "_rate_attempt", 0)
            if attempt < len(RATE_RETRY_DELAYS):
                import asyncio

                delay = RATE_RETRY_DELAYS[attempt] if self._real_transport() else 0
                log.warning("Playerok %s: слишком много запросов — жду %s с и повторяю", operation, delay)
                if self.on_wait is not None:
                    await self.on_wait(f"Playerok просит подождать — повтор через {delay} с")
                self._rate_attempt = attempt + 1
                try:
                    await asyncio.sleep(delay)
                    return await self._gql(operation, query, variables, _retries=_retries)
                finally:
                    self._rate_attempt = attempt
            raise PlayerokError("Playerok: слишком много запросов, попробуй через пару минут")
        if not isinstance(payload, dict):
            # HTML вместо JSON — это страница защиты от ботов, а не истёкшая сессия.
            snippet = " ".join(resp.text.split())[:300]
            log.warning("Playerok %s: HTTP %s, не JSON: %s", operation, resp.status, snippet)
            if resp.status in (403, 429, 503):
                raise PlayerokError(
                    f"защита Playerok заблокировала запрос (HTTP {resp.status})"
                )
            raise PlayerokError(f"Playerok вернул не JSON (HTTP {resp.status})")
        if resp.status == 401:
            raise AuthRequired("HTTP 401")

        errors = payload.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or "неизвестная ошибка")
            code = str(_get(first, "extensions", "code", default="")).upper()
            if "PERSISTED" in code or "PersistedQueryNotFound" in message:
                raise PlayerokError(
                    f"Playerok обновил сайт — сохранённый запрос {operation} устарел ({message}). "
                    "Нужно обновить хеш в bot/playerok/client.py (PERSISTED_QUERIES)."
                )
            if code in ("UNAUTHENTICATED", "FORBIDDEN") or "auth" in message.lower():
                raise AuthRequired(message)
            var_type = VAR_TYPE_RE.search(message)
            if var_type and _retries < MAX_FIELD_RETRIES:
                name, old, new = var_type.groups()
                patched = fix_variable_type(query, name, old, new)
                if patched != query:
                    log.warning("Playerok %s: тип $%s %s → %s", operation, name, old, new)
                    REMOVED_FIELDS.setdefault(operation, []).append(f"${name}: {old} -> {new}")
                    PATCHED_QUERIES[operation] = patched
                    return await self._gql(operation, patched, variables, _retries=_retries + 1)
            unknown = UNKNOWN_FIELD_RE.search(message)
            if unknown and _retries < MAX_FIELD_RETRIES:
                field = unknown.group(1)
                patched = strip_field(query, field)
                if patched != query:
                    log.warning("Playerok %s: нет поля %r, убираю его из запроса", operation, field)
                    REMOVED_FIELDS.setdefault(operation, []).append(field)
                    PATCHED_QUERIES[operation] = patched
                    return await self._gql(operation, patched, variables, _retries=_retries + 1)
            raise PlayerokError(message)

        # После входа сайт отдаёт токен сессии в Set-Cookie.
        if resp.token:
            self._token = resp.token
        return payload.get("data") or {}

    # ----- авторизация -----

    async def request_email_code(self, email: str) -> None:
        await self._gql("getEmailAuthCode", q.GET_EMAIL_AUTH_CODE, {"email": email})

    async def confirm_email_code(self, email: str, code: str) -> Viewer:
        data = await self._gql(
            "checkEmailAuthCode",
            q.CHECK_EMAIL_AUTH_CODE,
            {"input": {"email": email, "code": code}},
        )
        if not self._token:
            raise PlayerokError("Playerok не вернул токен сессии. Проверь запрос checkEmailAuthCode.")
        raw = data.get("checkEmailAuthCode") or {}
        if not raw.get("id"):
            raw = (await self._gql("viewer", q.VIEWER)).get("viewer") or {}
        return Viewer.from_raw(raw)

    async def viewer(self) -> Viewer:
        raw = (await self._gql("viewer", q.VIEWER)).get("viewer")
        if not raw:
            raise AuthRequired("viewer пуст")
        return Viewer.from_raw(raw)

    # ----- сделки -----

    async def my_sales(
        self, user_id: str, statuses: tuple[str, ...] | None = None, limit: int = 30
    ) -> list[Deal]:
        """Продажи пользователя, свежие первыми. statuses=None — без фильтра по статусу."""
        deals, _ = await self.sales_page(user_id, statuses=statuses, limit=limit)
        return deals

    async def sales_page(
        self,
        user_id: str,
        *,
        statuses: tuple[str, ...] | None = None,
        limit: int = 30,
        after: str | None = None,
    ) -> tuple[list[Deal], str | None]:
        """Одна страница продаж и курсор следующей (None — страниц больше нет)."""
        filt: dict[str, Any] = {"userId": user_id, "direction": "OUT"}
        if statuses:
            filt["status"] = list(statuses)
        pagination: dict[str, Any] = {"first": limit}
        if after:
            pagination["after"] = after
        data = await self._gql("deals", q.DEALS, {"pagination": pagination, "filter": filt})
        edges = _get(data, "deals", "edges", default=[]) or []
        deals = [Deal.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]
        has_next = bool(_get(data, "deals", "pageInfo", "hasNextPage", default=False))
        cursor = _get(data, "deals", "pageInfo", "endCursor")
        return deals, (cursor if has_next and cursor and deals else None)

    async def all_sales(self, user_id: str, max_pages: int = 100) -> list[Deal]:
        """Вся история продаж: листает страницы, пока они есть."""
        result: list[Deal] = []
        seen: set[str] = set()
        after: str | None = None
        for _ in range(max_pages):
            deals, after = await self.sales_page(user_id, limit=50, after=after)
            fresh = [d for d in deals if d.id and d.id not in seen]
            seen.update(d.id for d in fresh)
            result.extend(fresh)
            if not after or not fresh:
                break
        return result

    async def update_deal_status(self, deal_id: str, status: str) -> None:
        await self._gql("updateDeal", q.UPDATE_DEAL, {"input": {"id": deal_id, "status": status}})

    async def confirm_deal(self, deal_id: str) -> None:
        """Продавец отмечает заказ выполненным (выдан)."""
        await self.update_deal_status(deal_id, "SENT")

    # ----- лоты -----

    async def my_items(
        self, user_id: str, limit: int = 100, statuses: list[str] | None = None
    ) -> list[Item]:
        """Свои лоты (как в PlayerokAPI.get_my_items): сохранённый запрос `items`,
        фильтр {userId, status}, по 24 за страницу."""
        result: list[Item] = []
        after: str | None = None
        while len(result) < limit:
            data = await self._gql(
                "items",
                "persisted:" + PERSISTED_QUERIES["items"],
                {
                    "pagination": {"first": min(24, limit - len(result)), "after": after},
                    "filter": {"userId": user_id, "status": statuses or None},
                    "showForbiddenImage": True,
                },
            )
            edges = _get(data, "items", "edges", default=[]) or []
            result += [Item.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]
            page = _get(data, "items", "pageInfo", default={}) or {}
            after = page.get("endCursor")
            if not edges or not page.get("hasNextPage") or not after:
                break
        return result

    async def category_items(
        self, category_id: str, pages: int = 5, obtaining_type_id: str | None = None,
        on_page: Any = None,
    ) -> list[Item]:
        """Лоты всех продавцов в категории (как PlayerokAPI.get_items), только APPROVED.
        obtaining_type_id — только с этим способом получения (код / по нику и т.п.).
        Если Playerok начал отказывать на 2-й и дальше странице — возвращает собранное
        (self.partial = True), а не теряет всё."""
        result: list[Item] = []
        after: str | None = None
        self.partial = False
        filt: dict[str, Any] = {"gameCategoryId": category_id, "status": ["APPROVED"]}
        if obtaining_type_id:
            filt["obtainingTypeId"] = obtaining_type_id
        for page_no in range(1, pages + 1):
            try:
                data = await self._gql(
                    "items",
                    "persisted:" + PERSISTED_QUERIES["items"],
                    {
                        "pagination": {"first": 24, "after": after},
                        "filter": filt,
                    },
                )
            except PlayerokError:
                if not result:
                    raise
                self.partial = True
                break
            if on_page is not None:
                await on_page(page_no, pages, len(result) + len(_get(data, "items", "edges", default=[]) or []))
            edges = _get(data, "items", "edges", default=[]) or []
            result += [Item.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]
            page = _get(data, "items", "pageInfo", default={}) or {}
            after = page.get("endCursor")
            if not edges or not page.get("hasNextPage") or not after:
                break
        return result

    async def update_item_price(self, item_id: str, raw_price: int) -> dict[str, Any]:
        """Новая цена лота. Как в PlayerokAPI.update_item: input.price — цена продавца
        (rawPrice, без комиссии площадки). Возвращает лот из ответа (price, rawPrice, status)."""
        data = await self._gql("updateItem", q.UPDATE_ITEM, {"input": {"id": item_id, "price": int(raw_price)}})
        return _get(data, "updateItem") or {}

    async def find_sold_item(self, user_id: str, name: str) -> Item | None:
        """Проданный лот по названию. Так делает Playerok Universal: ID лота в сделке —
        не тот лот, который нужно выставлять заново (republish по нему отвечал 404)."""
        sold = await self.my_items(user_id, limit=48, statuses=["SOLD"])
        exact = [i for i in sold if i.name == name]
        if exact:
            return exact[0]
        target = " ".join(name.lower().split())
        return next((i for i in sold if " ".join(i.name.lower().split()) == target), None)

    async def get_item(self, item_id: str | None = None, slug: str | None = None) -> Item:
        data = await self._gql(
            "item",
            "persisted:" + PERSISTED_QUERIES["item"],
            {"id": item_id, "slug": slug, "hasSupportAccess": False, "showForbiddenImage": True},
        )
        raw = _get(data, "item")
        if not isinstance(raw, dict) or not raw.get("id"):
            raise PlayerokError("Playerok не вернул лот")
        return Item.from_raw(raw)

    async def priority_statuses(self, item_id: str, raw_price: float) -> list[dict[str, Any]]:
        data = await self._gql(
            "itemPriorityStatuses",
            "persisted:" + PERSISTED_QUERIES["itemPriorityStatuses"],
            {"itemId": item_id, "price": int(raw_price)},
        )
        return [s for s in (_get(data, "itemPriorityStatuses") or []) if isinstance(s, dict)]

    async def bump_item(self, item_id: str, *, max_cost: float | None = None) -> float:
        """Поднятие = платный статус PREMIUM (как increase_item_priority_status в PlayerokAPI).
        Возвращает цену. Если дороже max_cost — не поднимает."""
        item = await self.get_item(item_id)
        statuses = await self.priority_statuses(item.id, item.raw_price or item.price or 0)
        prem = next((s for s in statuses if str(s.get("type")).upper() == "PREMIUM" or (s.get("price") or 0) > 0), None)
        if prem is None:
            raise PlayerokError("Playerok не предложил статус для поднятия")
        cost = float(prem.get("price") or 0)
        if max_cost is not None and cost > max_cost:
            raise PaidPlacementRefused(f"поднятие стоит {cost:g} ₽ — больше лимита {max_cost:g} ₽")
        await self._gql(
            "increaseItemPriorityStatus",
            q.INCREASE_ITEM_PRIORITY,
            {"input": {
                "itemId": item.id,
                "priorityStatuses": [prem["id"]],
                "transactionProviderData": {"paymentMethodId": None},
                "transactionProviderId": "LOCAL",
            }},
        )
        return cost

    async def _rest_post(self, path: str) -> Any:
        """POST в REST-часть сайта (/rest-api/public/...) тем же транспортом и cookie.
        Ошибки — только PlayerokError: сессию по REST-ответу не сбрасываем."""
        body = {"operationName": "rest:" + path, "rest": path, "variables": {}}
        send = self._transport or self._curl_post
        if self._real_transport():
            await _rate_wait(self._token)
        try:
            resp = await send(body, self._token)
        except Exception as e:
            raise PlayerokError(f"Playerok недоступен: {e.__class__.__name__}") from e
        try:
            payload = json.loads(resp.text) if resp.text.strip() else None
        except ValueError:
            payload = None
        failed = isinstance(payload, dict) and (
            payload.get("success") is False or (payload.get("errors") and not payload.get("data"))
        )
        if 200 <= resp.status < 300 and not failed:
            return payload
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("message") or payload.get("error") or "")
            errs = payload.get("errors")
            if not msg and isinstance(errs, list) and errs and isinstance(errs[0], dict):
                msg = str(errs[0].get("message") or "")
        if not msg:
            msg = " ".join(resp.text.split())[:200] or "пустой ответ"
        raise PlayerokError(f"HTTP {resp.status}: {msg}")

    async def discontinue_item(self, item_id: str) -> None:
        """Снять лот с продажи (REST /item/{id}/discontinue, как PlayerokAPI.items.discontinue)."""
        await self._rest_post(f"/rest-api/public/item/{item_id}/discontinue")

    async def republish_item(self, item_id: str) -> None:
        """«Выставить снова» проданный/снятый лот — так делает кнопка на сайте
        (REST /item/{id}/republish, как в библиотеке PlayerokAPI)."""
        await self._rest_post(f"/rest-api/public/item/{item_id}/republish")

    async def publish_item(
        self,
        item_id: str,
        *,
        price: float | None = None,
        allow_paid: bool = False,
        slug: str | None = None,
        sold_name: str | None = None,
        user_id: str | None = None,
    ) -> str:
        """Выставляет лот заново — по схеме Playerok Universal:
        1) после продажи ищем свой лот со статусом SOLD по названию (sold_name);
        2) сохранённый запрос `item` → rawPrice, priority, mayBePublished;
        3) `itemPriorityStatuses` по rawPrice → тариф (DEFAULT, а для PREMIUM-лота — PREMIUM);
        4) publishItem {itemId, priorityStatuses:[id], transactionProviderId: LOCAL}.
        Каждая ошибка подписана шагом. Возвращает ID выставленного лота."""
        step = "поиск проданного лота"
        try:
            if sold_name and user_id:
                try:
                    found = await self.find_sold_item(user_id, sold_name)
                except (AuthRequired, PlayerokError) as e:
                    found = None
                    log.info("publish: список проданных лотов недоступен (%s), беру ID из сделки", e)
                if found is not None:
                    item_id = found.id
                else:
                    log.info("publish: проданный лот «%s» не найден среди SOLD, беру ID из сделки", sold_name)
            step = "лот"
            try:
                item = await self.get_item(item_id)
            except PlayerokError:
                if not slug:
                    raise
                item = await self.get_item(slug=slug)
            if item.may_be_published is False:
                raise PlayerokError(
                    "Playerok не даёт выставить этот лот повторно (так бывает в некоторых категориях) — "
                    "создай лот заново вручную"
                )
            raw_price = item.raw_price or item.price or price
            if not raw_price:
                raise PlayerokError("Playerok не отдал цену лота")
            step = "тарифы"
            statuses = await self.priority_statuses(item.id, raw_price)
            premium = item.priority.upper() == "PREMIUM"
            if premium:
                # премиум-лот Playerok не выставляет с бесплатным статусом
                status = next((s for s in statuses if str(s.get("type")).upper() == "PREMIUM"
                               or (s.get("price") or 0) > 0), None)
            else:
                status = next((s for s in statuses if str(s.get("type")).upper() == "DEFAULT"
                               or (s.get("price") or 0) == 0), None) or (statuses[0] if statuses else None)
            if status is None:
                raise PlayerokError("Playerok не предложил статус размещения")
            cost = float(status.get("price") or 0)
            if cost and not allow_paid:
                raise PaidPlacementRefused(
                    f"размещение платное ({cost:g} ₽), а платное восстановление выключено в настройках"
                )
            step = "publishItem"
            await self._gql(
                "publishItem",
                q.PUBLISH_ITEM,
                {"input": {
                    "transactionProviderId": "LOCAL",
                    "priorityStatuses": [status["id"]],
                    "itemId": item.id,
                }},
            )
            return item.id
        except PaidPlacementRefused:
            raise
        except AuthRequired:
            raise
        except PlayerokError as e:
            raise PlayerokError(f"{step}: {e}") from e

    # ----- чаты -----

    async def chats(self, user_id: str, limit: int = 30) -> list[ChatPreview]:
        data = await self._gql(
            "chats",
            q.CHATS,
            {"pagination": {"first": limit}, "filter": {"userId": user_id}},
        )
        edges = _get(data, "chats", "edges", default=[]) or []
        return [ChatPreview.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]

    async def mark_chat_read(self, chat_id: str) -> None:
        await self._gql("markChatAsRead", q.MARK_CHAT_AS_READ, {"input": {"chatId": chat_id}})

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._gql(
            "createChatMessage",
            q.CREATE_CHAT_MESSAGE,
            {"input": {"chatId": chat_id, "text": text}},
        )


_ORIGINAL_CURL_POST = PlayerokClient._curl_post
