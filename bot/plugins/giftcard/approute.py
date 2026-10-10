"""Клиент AppRoute Public API (https://approute.io/api/v1, для России — approute.ru).

Только то, что есть в официальном SDK (github.com/AppRoute-FZCO/AppRoute-Public-API-SDK)
и гайде AppRoute:
  GET  /accounts                         балансы (USDT)
  GET  /services                         каталог: товары (services) и их номиналы (items)
  GET  /services/{id}                    один товар с номиналами
  POST /orders                           покупка: ordersType=shop, referenceId (идемпотентность)
  GET  /orders?referenceId=…&unhide=true заказ с полными кодами
Ответ — конверт {status, code|statusCode, message|statusMessage, traceId, data, errors}.
Ключ уходит только в заголовке X-API-Key и не попадает в логи и тексты ошибок.
Постоянный ключ AppRoute работает только с разрешённых IPv4 — поэтому запросы идут через
прокси (APPROUTE_PROXY, иначе PLAYEROK_PROXY): его IP добавляется в белый список ключа.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any

from .client import FazerAuthError, FazerError, FazerUnknownResult, Transport

log = logging.getLogger(__name__)

BASE_URLS = {"io": "https://approute.io/api/v1", "ru": "https://approute.ru/api/v1"}
SUCCESS_CODES = {"OK", "ACCEPTED", "IDEMPOTENCY_REPLAY"}
# числовые statusCode из гайда AppRoute
NUMERIC_CODES = {0: "OK", 1: "ACCEPTED", 2: "IDEMPOTENCY_REPLAY", 8: "LIMIT_REACHED", 9: "OUT_OF_STOCK",
                 10: "INSUFFICIENT_FUNDS", 13: "API_KEY_LIMIT_EXCEEDED"}
# при покупке: не знаем, прошла ли — повтор только с тем же referenceId (вернёт первый результат)
UNKNOWN_CODES = {"LIMIT_REACHED", "UPSTREAM_ERROR", "INTERNAL_ERROR", "CONFLICT"}
RU_ERRORS = {
    "OUT_OF_STOCK": "нет в наличии",
    "INSUFFICIENT_FUNDS": "недостаточно средств на балансе AppRoute",
    "UNAUTHORIZED": "ключ не принят — проверь ключ и белый список IP",
    "FORBIDDEN": "доступ запрещён — проверь права ключа и белый список IP",
    "API_KEY_LIMIT_EXCEEDED": "исчерпан лимит трат API-ключа",
    "LIMIT_REACHED": "слишком много запросов",
    "NOT_FOUND": "не найдено",
    "VALIDATION_ERROR": "неверные данные заказа",
}
_NS = uuid.UUID("6f1c2a7e-3b0d-4c55-9a8e-5d1f0b7c9e21")


def reference_for(key: str) -> str:
    """referenceId из ключа заказа бота: всегда один и тот же для одного заказа (UUID, ≤40 символов)."""
    return str(uuid.uuid5(_NS, key))


def proxy_from_env() -> str | None:
    p = os.getenv("APPROUTE_PROXY", "").strip()
    if p.lower() == "none":
        return None
    return p or os.getenv("PLAYEROK_PROXY", "").strip() or None


def base_url(region: str) -> str:
    env = os.getenv("APPROUTE_API_URL", "").strip()
    return (env or BASE_URLS.get(region, BASE_URLS["io"])).rstrip("/")


def _code(payload: dict[str, Any]) -> str:
    c = payload.get("code", payload.get("statusCode"))
    if isinstance(c, int) or (isinstance(c, str) and c.isdigit()):
        return NUMERIC_CODES.get(int(c), str(c))
    return str(c or "").upper()


def _is_validation(e: FazerError) -> bool:
    """Ошибка проверки данных запроса: заказ не создан, денег не списано."""
    return e.code == "VALIDATION_ERROR" or "validation error" in str(e).lower()


def norm_status(value: Any) -> str:
    """in_progress / IN_PROGRESS → IN_PROGRESS; completed → SUCCESS."""
    s = str(value or "").upper()
    return "SUCCESS" if s == "COMPLETED" else s


def voucher_codes(vouchers: Any) -> tuple[list[str], bool]:
    """(коды для покупателя, есть ли среди них скрытые «****1234»)."""
    codes: list[str] = []
    masked = False
    for v in vouchers or []:
        if not isinstance(v, dict):
            continue
        pin = str(v.get("pin") or "").strip()
        if not pin:
            continue
        masked = masked or pin.startswith("****")
        serial = str(v.get("serialNumber") or v.get("serial_number") or "").strip()
        codes.append(pin + (f"\nСерийный номер: {serial}" if serial else ""))
    return codes, masked


class AppRouteClient:
    def __init__(
        self, api_key: str, *, region: str = "io", timeout: float = 25.0, transport: Transport | None = None,
    ) -> None:
        self._key = api_key
        self._base = base_url(region)
        self._timeout = timeout
        self._transport = transport
        self._session: Any = None

    async def aclose(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "AppRouteClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _curl(
        self, method: str, url: str, headers: dict[str, str], params: dict[str, str] | None, body: dict[str, Any] | None
    ) -> tuple[int, str]:
        from curl_cffi.requests import AsyncSession

        if self._session is None:
            self._session = AsyncSession(timeout=self._timeout, proxy=proxy_from_env())
        resp = await self._session.request(method, url, headers=headers, params=params, json=body)
        return resp.status_code, resp.text

    async def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None, purchase: bool = False,
    ) -> Any:
        if not self._key:
            raise FazerAuthError("AppRoute: не задан API-ключ")
        headers = {"X-API-Key": self._key, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"  # без него AppRoute отвечает 415
        send = self._transport or self._curl
        try:
            status, text = await send(method, self._base + path, headers, params, body)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # сеть, тайм-аут, TLS, прокси
            name = e.__class__.__name__
            if purchase:
                raise FazerUnknownResult(f"AppRoute: нет ответа ({name})", transient=True) from e
            raise FazerError(f"AppRoute недоступен ({name})", transient=True) from e

        try:
            payload = json.loads(text) if text.strip() else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            msg = f"AppRoute: некорректный ответ (HTTP {status}, не JSON)"
            if purchase and (status >= 500 or 200 <= status < 300):
                raise FazerUnknownResult(msg, status=status, transient=True)
            raise FazerError(msg, status=status, transient=status >= 500)

        code = _code(payload)
        if 200 <= status < 300 and (code in SUCCESS_CODES or (not code and "data" in payload)):
            return payload.get("data")

        message = str(payload.get("message") or payload.get("statusMessage") or "")
        fields = "; ".join(
            f"{e.get('field')}: {e.get('message') or e.get('code')}"
            for e in payload.get("errors") or [] if isinstance(e, dict)
        )
        human = RU_ERRORS.get(code) or message or f"HTTP {status}"
        full = f"AppRoute: {human}" + (f" ({message})" if message and message != human else "")
        full += (f" [{fields}]" if fields else "") + (f" [{code}]" if code else "")
        if status in (401, 403) or code in ("UNAUTHORIZED", "FORBIDDEN"):
            raise FazerAuthError(full, status=status, code=code)
        if purchase and (status >= 500 or status == 429 or code in UNKNOWN_CODES):
            raise FazerUnknownResult(full, status=status, code=code, transient=True)
        raise FazerError(full, status=status, code=code, transient=status >= 500 or status == 429)

    async def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET безопасно повторять: до 2 повторов при временных ошибках."""
        delay = 1.0
        for attempt in range(3):
            try:
                return await self._request("GET", path, params=params)
            except FazerError as e:
                if not e.transient or isinstance(e, FazerAuthError) or attempt == 2:
                    raise
                log.info("AppRoute GET %s: %s — повтор через %.0f с", path, e, delay)
                await asyncio.sleep(delay if self._transport is None else 0)
                delay *= 2
        raise AssertionError("unreachable")

    # ----- методы API -----

    async def balance(self) -> tuple[str, str]:
        data = await self._get("/accounts")
        items = (data or {}).get("items") or []
        acc = next((a for a in items if isinstance(a, dict)), None)
        if acc is None:
            return "0", "USDT"
        return str(acc.get("available", acc.get("balance"))), str(acc.get("currency") or "USDT")

    async def services(self) -> list[dict[str, Any]]:
        data = await self._get("/services")
        return [p for p in (data or {}).get("items") or [] if isinstance(p, dict)]

    async def service(self, product_id: str) -> dict[str, Any]:
        data = await self._get(f"/services/{product_id}")
        if not isinstance(data, dict):
            raise FazerError("AppRoute: в ответе нет товара")
        return data

    async def order(self, product_id: str, item_id: str, quantity: int, reference_id: str) -> dict[str, Any]:
        """Покупка кода (shop). Без автоповторов: повтор — только с тем же referenceId.

        Тело — массив orders с denominationId (гайд AppRoute). Поля productId/itemId/quantity/
        clientTime на верхнем уровне (как в SDK) AppRoute вживую отклонил: «orders: Field
        required; productId: Extra inputs are not permitted». Если не примет denominationId —
        один раз itemId: ошибка проверки данных значит, что заказ не создан и денег не списано.
        """
        def body(key: str) -> dict[str, Any]:
            return {"ordersType": "shop", "referenceId": reference_id,
                    "orders": [{key: item_id, "quantity": quantity}]}

        try:
            data = await self._request("POST", "/orders", body=body("denominationId"), purchase=True)
        except (FazerUnknownResult, FazerAuthError):
            raise
        except FazerError as e:
            text = str(e)
            if not (_is_validation(e) and ("denominationId" in text or "itemId" in text)):
                raise
            log.info("AppRoute: denominationId не принят (%s) — пробую itemId", text[:300])
            data = await self._request("POST", "/orders", body=body("itemId"), purchase=True)
        if not isinstance(data, dict):
            raise FazerUnknownResult("AppRoute: в ответе на покупку нет заказа", transient=True)
        return data

    async def find_order(self, reference_id: str) -> dict[str, Any] | None:
        """Заказ по referenceId с полными кодами (unhide=true помечает коды полученными)."""
        data = await self._get("/orders", {"referenceId": reference_id, "unhide": "true", "limit": "1", "offset": "0"})
        items = ((data or {}).get("page") or {}).get("items") or []
        return next((o for o in items if isinstance(o, dict)), None)
