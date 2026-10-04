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


HEADERS = {
    "Accept": "*/*",
    "Content-Type": "application/json",
    "Origin": BASE_URL,
    "Referer": BASE_URL + "/",
    "apollo-require-preflight": "true",
}


class PlayerokClient:
    def __init__(
        self, token: str | None = None, timeout: float = 20.0, transport: Transport | None = None
    ) -> None:
        self._token = token
        self._timeout = timeout
        self._transport = transport
        self._session: Any = None

    async def _curl_post(self, body: dict[str, Any], token: str | None) -> RawResponse:
        # curl_cffi повторяет TLS-отпечаток настоящего Chrome: без этого защита
        # Playerok от ботов отвечает 403 ещё до обработки запроса.
        from curl_cffi.requests import AsyncSession

        if self._session is None:
            # PLAYEROK_PROXY — прокси для запросов к Playerok (например, если сайт
            # не пускает IP хостинга): http://user:pass@host:port или socks5://...
            proxy = os.getenv("PLAYEROK_PROXY", "").strip() or None
            self._session = AsyncSession(impersonate="chrome", timeout=self._timeout, proxy=proxy)
        resp = await self._session.post(
            BASE_URL + "/graphql",
            json=body,
            headers=HEADERS,
            cookies={"token": token} if token else None,
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

    async def _gql(
        self,
        operation: str,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        _retries: int = 0,
    ) -> dict[str, Any]:
        # Если раньше из запроса уже выкидывали неизвестные поля — берём исправленный.
        query = PATCHED_QUERIES.get(operation, query)
        body = {"operationName": operation, "query": query, "variables": variables or {}}
        send = self._transport or self._curl_post
        try:
            resp = await send(body, self._token)
        except Exception as e:  # сеть, таймаут, TLS
            raise PlayerokError(f"Playerok недоступен: {e.__class__.__name__}") from e

        try:
            payload = json.loads(resp.text)
        except ValueError:
            payload = None
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

    # Playerok отвечал «Access denied» на фильтр {userId}. Форму фильтра
    # заранее не знаем — перебираем варианты и запоминаем рабочий.
    _items_filter: dict[str, Any] | None = None

    def _items_filter_variants(self, user_id: str) -> list[dict[str, Any]]:
        statuses = ["APPROVED", "SOLD", "EXPIRED", "DRAFT"]
        return [
            {"userId": user_id},
            {"userId": user_id, "status": statuses},
            {"sellerId": user_id},
            {"user": user_id},
            {"ownerId": user_id},
            {},
        ]

    async def my_items(self, user_id: str, limit: int = 100) -> list[Item]:
        variants = (
            [PlayerokClient._items_filter]
            if PlayerokClient._items_filter is not None
            else self._items_filter_variants(user_id)
        )
        last: Exception | None = None
        for filt in variants:
            try:
                data = await self._gql(
                    "items", q.MY_ITEMS, {"pagination": {"first": limit}, "filter": filt}
                )
            except (AuthRequired, PlayerokError) as e:
                # Любой вид фильтра может не подойти — пробуем следующий.
                last = e
                continue
            PlayerokClient._items_filter = filt  # запомнили рабочий
            edges = _get(data, "items", "edges", default=[]) or []
            return [Item.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]
        if last is not None:
            raise last
        return []

    async def bump_item(self, item_id: str) -> None:
        await self._gql(
            "increaseItemPriorityStatus", q.INCREASE_ITEM_PRIORITY, {"input": {"itemId": item_id}}
        )

    async def item_price(self, item_id: str) -> float | None:
        data = await self._gql("item", q.ITEM_PRICE, {"id": item_id})
        price = _get(data, "item", "price")
        return float(price) if isinstance(price, (int, float)) else None

    async def publish_item(
        self, item_id: str, *, price: float | None = None, allow_paid: bool = False
    ) -> None:
        """Выставляет лот заново. Playerok требует тариф (priorityStatuses) и способ
        оплаты (transactionProviderId) даже для бесплатного размещения: берём тариф
        DEFAULT, оплата LOCAL (с баланса) — при бесплатном тарифе списаний нет."""
        try:
            price = await self.item_price(item_id) or price
        except PlayerokError:
            if price is None:
                raise
        if price is None:
            raise PlayerokError("Playerok не отдал цену лота — без неё не выбрать тариф размещения")
        data = await self._gql(
            "itemPriorityStatuses", q.ITEM_PRIORITY_STATUSES, {"itemId": item_id, "price": price}
        )
        statuses = [s for s in (_get(data, "itemPriorityStatuses") or []) if isinstance(s, dict)]
        free = next((s for s in statuses if str(s.get("type")).upper() == "DEFAULT"), None)
        if free is None:
            raise PlayerokError("Playerok не предложил обычный (бесплатный) тариф размещения")
        cost = free.get("price") or 0
        if cost and not allow_paid:
            raise PlayerokError(
                f"размещение платное ({cost:g} ₽), а платное восстановление выключено в настройках"
            )
        await self._gql(
            "publishItem",
            q.PUBLISH_ITEM,
            {"input": {
                "itemId": item_id,
                "priorityStatuses": [free["id"]],
                "transactionProviderId": "LOCAL",
            }},
        )

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
