"""Клиент FazerCards Public API (https://api.fzr.cards/public/docs).

Только HTTP: ничего не знает о Playerok и заказах бота. Используются лишь
эндпоинты из документации:
  GET  /api/v2/balance
  GET  /api/v2/giftcards                      (категории)
  GET  /api/v2/giftcards/cards?category_id=…  (номиналы категории)
  POST /api/v2/giftcards/order                (покупка, заголовок Idempotency-Key)
  GET  /api/v2/orders/{orderId}               (заказ по ord-N)
Ключ уходит только в заголовке X-API-Key и никогда не попадает в логи и ошибки.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.fzr.cards"
ORDER_ID_RE = re.compile(r"^ord-[0-9]+$")

# (метод, url, заголовки, query, json) → (HTTP-статус, тело). Подменяется в тестах.
Transport = Callable[
    [str, str, dict[str, str], "dict[str, str] | None", "dict[str, Any] | None"],
    Awaitable["tuple[int, str]"],
]


class FazerError(Exception):
    """Ошибка API. transient — можно безопасно повторить позже."""

    def __init__(self, message: str, *, status: int | None = None, code: str = "", transient: bool = False):
        super().__init__(message)
        self.status = status
        self.code = code
        self.transient = transient


class FazerAuthError(FazerError):
    """Неверный/отсутствующий ключ (401)."""


class FazerUnknownResult(FazerError):
    """Покупка отправлена, но итог неизвестен (тайм-аут, сеть, 5xx, мусор в ответе).
    Повторять можно только с тем же Idempotency-Key."""


def api_key_from_env() -> str:
    return os.getenv("FAZER_API_KEY", "").strip()


def base_url_from_env() -> str:
    return (os.getenv("FAZER_API_URL", "").strip() or DEFAULT_BASE_URL).rstrip("/")


class FazerCardsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout: float = 20.0,
        transport: Transport | None = None,
    ) -> None:
        self._key = api_key
        self._base = (base_url or base_url_from_env()).rstrip("/")
        self._timeout = timeout
        self._transport = transport
        self._session: Any = None

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "FazerCardsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ----- транспорт -----

    async def _aiohttp(
        self, method: str, url: str, headers: dict[str, str], params: dict[str, str] | None, body: dict[str, Any] | None
    ) -> tuple[int, str]:
        import aiohttp

        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self._timeout))
        async with self._session.request(method, url, headers=headers, params=params, json=body) as resp:
            return resp.status, await resp.text()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        purchase: bool = False,
    ) -> dict[str, Any]:
        if not self._key:
            raise FazerAuthError("не задан API-ключ (переменная FAZER_API_KEY)")
        headers = {"X-API-Key": self._key, "Accept": "application/json"}
        if extra_headers:
            headers.update(extra_headers)
        send = self._transport or self._aiohttp
        try:
            status, text = await send(method, self._base + path, headers, params, body)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # сеть, тайм-аут, TLS
            name = e.__class__.__name__
            if purchase:
                raise FazerUnknownResult(f"нет ответа от FazerCards ({name})", transient=True) from e
            raise FazerError(f"FazerCards недоступен ({name})", transient=True) from e

        try:
            payload = json.loads(text) if text.strip() else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            msg = f"некорректный ответ FazerCards (HTTP {status}, не JSON)"
            if purchase and (status >= 500 or 200 <= status < 300):
                raise FazerUnknownResult(msg, status=status, transient=True)
            raise FazerError(msg, status=status, transient=status >= 500)

        if 200 <= status < 300 and payload.get("ok") is True:
            return payload

        error = str(payload.get("error") or f"HTTP {status}")
        code = str(payload.get("code") or "")
        if payload.get("blockReason"):
            error += f" ({payload['blockReason']})"
        full = f"{error} [{code}]" if code else error
        if status == 401:
            raise FazerAuthError(full, status=status, code=code)
        if purchase and (status >= 500 or status == 409 or 200 <= status < 300):
            # 5xx/409 при покупке: не знаем, прошла ли она → только повтор с тем же ключом.
            raise FazerUnknownResult(full, status=status, code=code, transient=True)
        raise FazerError(full, status=status, code=code, transient=status >= 500 or status == 429)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """GET безопасно повторять: до 2 повторов при временных ошибках."""
        delay = 0.5
        for attempt in range(3):
            try:
                return await self._request("GET", path, params=params)
            except FazerError as e:
                if not e.transient or isinstance(e, FazerAuthError) or attempt == 2:
                    raise
                log.info("FazerCards GET %s: %s — повтор через %.1f с", path, e, delay)
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")

    # ----- методы API -----

    async def balance(self) -> tuple[str, str]:
        data = await self._get("/api/v2/balance")
        return str(data.get("balance")), str(data.get("currency") or "USD")

    async def categories(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(10):
            params = {"limit": "500"}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/api/v2/giftcards", params)
            items += [i for i in data.get("items") or [] if isinstance(i, dict)]
            meta = data.get("meta") or {}
            cursor = meta.get("next_cursor")
            if not meta.get("has_more") or not cursor:
                break
        return items

    async def offers(self, category_id: str) -> dict[str, Any]:
        return await self._get("/api/v2/giftcards/cards", {"category_id": category_id})

    async def order_giftcard(
        self, category_id: str, card_id: str, quantity: int, idempotency_key: str
    ) -> dict[str, Any]:
        """Покупка. Без автоповторов: повтор делает вызывающий код и только с тем же ключом."""
        data = await self._request(
            "POST",
            "/api/v2/giftcards/order",
            body={"category_id": category_id, "card_id": card_id, "quantity": quantity},
            extra_headers={"Idempotency-Key": idempotency_key[:255]},
            purchase=True,
        )
        order = data.get("order")
        if not isinstance(order, dict):
            raise FazerUnknownResult("в ответе на покупку нет заказа", transient=True)
        return order

    async def get_order(self, order_id: str) -> dict[str, Any]:
        if not ORDER_ID_RE.match(order_id or ""):
            raise FazerError(f"неверный ID заказа поставщика: {order_id!r}")
        data = await self._get(f"/api/v2/orders/{order_id}")
        order = data.get("order")
        if not isinstance(order, dict):
            raise FazerError("в ответе нет заказа", transient=True)
        return order
