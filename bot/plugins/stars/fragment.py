"""Клиент fragment.com для покупки Telegram Stars.

У Fragment нет публичного API: бот повторяет запросы, которые делает сайт,
от имени залогиненного аккаунта (cookies продавца). Все названия методов и
параметров собраны в METHODS — если Fragment что-то поменяет, править здесь.
Ответы сервера при ошибке пробрасываются в текст исключения, чтобы их было
видно в Telegram.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

BASE_URL = "https://fragment.com"
HASH_RE = re.compile(r'apiUrl["\']?\s*:\s*["\']\\?/api\?hash=([0-9a-f]+)')
HASH_RE_ALT = re.compile(r"/api\?hash=([0-9a-f]{8,})")
LOGGED_IN_RE = re.compile(r'"tgUser"|tm-header-avatar|\bstel_token\b|logout', re.I)

# Параметры API Fragment. Сверить при первой реальной покупке.
METHODS = {
    "search": "searchStarsRecipient",      # query, quantity → found.recipient
    "init": "initBuyStarsRequest",         # recipient, quantity → req_id, amount
    "link": "getBuyStarsLink",             # id, transaction=1, show_sender=0 → transaction.messages
    "state": "updateStarsBuyState",        # id, mode=new, lv=false → ok / error
}
# Как Fragment называет оплату USDT в сети TON (строка из выпадающего списка).
USDT_TON_CURRENCY = os.getenv("FRAGMENT_USDT_CURRENCY", "usdt_ton")


class FragmentError(Exception):
    pass


class FragmentAuthError(FragmentError):
    """Cookies невалидны: нужно заново скопировать их из браузера."""


class RecipientNotFound(FragmentError):
    """Такого @username нет на Fragment (или профиль закрыт)."""


NOT_FOUND_MARKERS = ("not found", "no telegram users", "не найден", "invalid username", "no user")


@dataclass(frozen=True)
class Recipient:
    id: str
    name: str
    photo: str = ""


@dataclass(frozen=True)
class StarsInvoice:
    req_id: str
    amount_text: str  # цена, как показывает Fragment (например «0.485 TON» или «1.95 USDT»)
    messages: list[dict[str, Any]] = field(default_factory=list)  # [{address, amount, payload, stateInit}]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawResponse:
    status: int
    text: str


Transport = Callable[[str, dict[str, Any] | None, dict[str, str]], Awaitable[RawResponse]]


def parse_cookies(raw: str) -> dict[str, str]:
    """Принимает строку cookies из браузера («a=1; b=2»), JSON (Cookie-Editor)
    или строки вида «name<TAB>value». Возвращает словарь."""
    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("["):
        try:
            items = json.loads(raw)
            return {str(i["name"]): str(i["value"]) for i in items if "name" in i and "value" in i}
        except (ValueError, TypeError, KeyError):
            pass
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            return {str(k): str(v) for k, v in data.items()}
        except (ValueError, AttributeError):
            pass
    result: dict[str, str] = {}
    for part in re.split(r";|\n", raw):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        elif "\t" in part:
            k, v = part.split("\t", 1)
        else:
            continue
        result[k.strip()] = v.strip().strip('"')
    return result


class FragmentClient:
    def __init__(
        self, cookies: dict[str, str], timeout: float = 25.0, transport: Transport | None = None
    ) -> None:
        self._cookies = cookies
        self._timeout = timeout
        self._transport = transport
        self._session: Any = None
        self._hash: str | None = None

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "FragmentClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----- транспорт -----

    async def _curl(self, url: str, data: dict[str, Any] | None, headers: dict[str, str]) -> RawResponse:
        from curl_cffi.requests import AsyncSession

        if self._session is None:
            proxy = os.getenv("FRAGMENT_PROXY", "").strip() or None
            self._session = AsyncSession(impersonate="chrome", timeout=self._timeout, proxy=proxy)
        if data is None:
            resp = await self._session.get(url, headers=headers, cookies=self._cookies)
        else:
            resp = await self._session.post(url, data=data, headers=headers, cookies=self._cookies)
        return RawResponse(resp.status_code, resp.text)

    async def _request(self, url: str, data: dict[str, Any] | None = None) -> RawResponse:
        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01" if data else "text/html,*/*",
            "Referer": BASE_URL + "/stars/buy",
            "Origin": BASE_URL,
        }
        if data is not None:
            headers["X-Requested-With"] = "XMLHttpRequest"
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        send = self._transport or self._curl
        try:
            return await send(url, data, headers)
        except Exception as e:
            raise FragmentError(f"Fragment недоступен: {e.__class__.__name__}") from e

    async def _get_hash(self) -> str:
        if self._hash:
            return self._hash
        resp = await self._request(BASE_URL + "/stars/buy")
        if resp.status in (401, 403):
            raise FragmentAuthError(f"Fragment не пустил на страницу (HTTP {resp.status})")
        m = HASH_RE.search(resp.text) or HASH_RE_ALT.search(resp.text)
        if not m:
            snippet = " ".join(resp.text.split())[:200]
            raise FragmentError(f"не нашёл api hash на странице Fragment (HTTP {resp.status}): {snippet}")
        if not LOGGED_IN_RE.search(resp.text):
            raise FragmentAuthError("похоже, сессия Fragment не авторизована: скопируй cookies заново")
        self._hash = m.group(1)
        return self._hash

    async def _api(self, method: str, **params: Any) -> dict[str, Any]:
        h = await self._get_hash()
        data = {"method": method, **{k: v for k, v in params.items() if v is not None}}
        resp = await self._request(f"{BASE_URL}/api?hash={h}", data)
        try:
            payload = json.loads(resp.text)
        except ValueError:
            snippet = " ".join(resp.text.split())[:200]
            if resp.status in (401, 403):
                raise FragmentAuthError(f"Fragment отклонил запрос {method} (HTTP {resp.status})")
            raise FragmentError(f"Fragment {method}: не JSON (HTTP {resp.status}): {snippet}")
        if not isinstance(payload, dict):
            raise FragmentError(f"Fragment {method}: неожиданный ответ: {str(payload)[:200]}")
        if payload.get("error"):
            err = str(payload["error"])
            if "auth" in err.lower() or "login" in err.lower() or "unauthorized" in err.lower():
                raise FragmentAuthError(f"Fragment {method}: {err}")
            raise FragmentError(f"Fragment {method}: {err}")
        return payload

    # ----- публичные методы -----

    async def check_session(self) -> bool:
        await self._get_hash()
        return True

    async def search_recipient(self, username: str, quantity: int) -> Recipient:
        username = username.lstrip("@").strip()
        try:
            data = await self._api(METHODS["search"], query=username, quantity=quantity)
        except FragmentAuthError:
            raise
        except FragmentError as e:
            if any(m in str(e).lower() for m in NOT_FOUND_MARKERS):
                raise RecipientNotFound(f"@{username}: {e}") from e
            raise
        found = data.get("found") or {}
        if not found.get("recipient"):
            raise RecipientNotFound(f"пользователь @{username} не найден на Fragment: {str(data)[:200]}")
        return Recipient(
            id=str(found["recipient"]),
            name=str(found.get("name") or username),
            photo=str(found.get("photo") or ""),
        )

    async def create_invoice(self, recipient: Recipient, quantity: int) -> StarsInvoice:
        init = await self._api(METHODS["init"], recipient=recipient.id, quantity=quantity)
        req_id = init.get("req_id") or init.get("id")
        if not req_id:
            raise FragmentError(f"Fragment не вернул req_id: {str(init)[:300]}")
        link = await self._api(
            METHODS["link"],
            id=req_id,
            transaction=1,
            show_sender=0,
            currency=USDT_TON_CURRENCY,
        )
        tx = link.get("transaction") or {}
        messages = tx.get("messages") or []
        if not messages:
            raise FragmentError(
                f"Fragment не вернул транзакцию для оплаты (нужен подключённый кошелёк?): {str(link)[:300]}"
            )
        amount = str(init.get("amount") or link.get("amount") or "")
        return StarsInvoice(req_id=str(req_id), amount_text=amount, messages=messages, raw={**init, **link})

    async def check_state(self, req_id: str) -> dict[str, Any]:
        return await self._api(METHODS["state"], id=req_id, mode="new", lv="false")
