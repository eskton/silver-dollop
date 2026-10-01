"""Асинхронный клиент Playerok поверх его GraphQL-эндпоинта.

Сессия Playerok живёт в cookie `token`. Клиент либо получает её при входе
по почте (`request_email_code` → `confirm_email_code`), либо создаётся
с уже сохранённым токеном продавца.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from . import queries as q
from .errors import AuthRequired, PlayerokError

BASE_URL = "https://playerok.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
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
    item_name: str
    price: float | None
    buyer_id: str
    buyer_username: str
    chat_id: str | None
    created_at: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "Deal":
        return cls(
            id=str(_get(raw, "id", default="")),
            status=str(_get(raw, "status", default="")),
            item_name=str(_get(raw, "item", "name", default="товар")),
            price=_get(raw, "item", "price"),
            buyer_id=str(_get(raw, "user", "id", default="")),
            buyer_username=str(_get(raw, "user", "username", default="покупатель")),
            chat_id=_get(raw, "chat", "id"),
            created_at=str(_get(raw, "createdAt", default="")),
        )


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


class PlayerokClient:
    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        self._token = token
        self._http = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
                "apollo-require-preflight": "true",
            },
        )
        if token:
            self._set_cookie(token)

    def _set_cookie(self, token: str) -> None:
        self._http.cookies.set("token", token, domain="playerok.com", path="/")

    @property
    def token(self) -> str | None:
        return self._token

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "PlayerokClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----- низкий уровень -----

    async def _gql(
        self, operation: str, query: str, variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            resp = await self._http.post(
                "/graphql",
                json={"operationName": operation, "query": query, "variables": variables or {}},
            )
        except httpx.HTTPError as e:
            raise PlayerokError(f"Playerok недоступен: {e.__class__.__name__}") from e

        if resp.status_code in (401, 403):
            raise AuthRequired(f"HTTP {resp.status_code}")
        try:
            payload = resp.json()
        except ValueError as e:
            raise PlayerokError(f"Playerok вернул не JSON (HTTP {resp.status_code})") from e

        errors = payload.get("errors") or []
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            message = str(first.get("message") or "неизвестная ошибка")
            code = str(_get(first, "extensions", "code", default="")).upper()
            if code in ("UNAUTHENTICATED", "FORBIDDEN") or "auth" in message.lower():
                raise AuthRequired(message)
            raise PlayerokError(message)

        # После входа сайт отдаёт токен сессии в Set-Cookie; httpx сам кладёт
        # его в cookie-хранилище клиента, нам остаётся только запомнить.
        new_token = resp.cookies.get("token")
        if new_token:
            self._token = new_token
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
        self, user_id: str, statuses: tuple[str, ...] = ACTIVE_SALE_STATUSES, limit: int = 20
    ) -> list[Deal]:
        data = await self._gql(
            "deals",
            q.DEALS,
            {
                "pagination": {"first": limit},
                "filter": {"userId": user_id, "direction": "OUT", "status": list(statuses)},
            },
        )
        edges = _get(data, "deals", "edges", default=[]) or []
        return [Deal.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]

    async def update_deal_status(self, deal_id: str, status: str) -> None:
        await self._gql("updateDeal", q.UPDATE_DEAL, {"input": {"id": deal_id, "status": status}})

    # ----- чаты -----

    async def chats(self, user_id: str, limit: int = 30) -> list[ChatPreview]:
        data = await self._gql(
            "chats",
            q.CHATS,
            {"pagination": {"first": limit}, "filter": {"userId": user_id}},
        )
        edges = _get(data, "chats", "edges", default=[]) or []
        return [ChatPreview.from_raw(e.get("node") or {}) for e in edges if isinstance(e, dict)]

    async def send_message(self, chat_id: str, text: str) -> None:
        await self._gql(
            "createChatMessage",
            q.CREATE_CHAT_MESSAGE,
            {"input": {"chatId": chat_id, "text": text}},
        )
