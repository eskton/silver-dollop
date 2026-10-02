"""Кошелёк TON продавца: баланс TON и USDT, отправка транзакций Fragment.

Fragment отдаёт готовые сообщения в формате TON Connect
({address, amount, payload, stateInit}), бот их только подписывает и
отправляет. Для USDT это перевод жетона — Fragment сам строит payload.
"""

from __future__ import annotations

import base64
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# USDT (Tether) в сети TON
USDT_MASTER = "EQCxE6mUtQJKFnGfaROTKOt1lZbDiiX1kCixRv7Nw2Id_sDs"
USDT_DECIMALS = 6
NANO = 10**9


class TonWalletError(Exception):
    pass


@dataclass(frozen=True)
class Balances:
    ton: float
    usdt: float


class TonWallet:
    def __init__(self, mnemonic: str, version: str = "v5r1") -> None:
        self._mnemonic = mnemonic.strip()
        self._version = version
        self._client: Any = None
        self._wallet: Any = None

    @staticmethod
    def validate_mnemonic(mnemonic: str) -> list[str]:
        words = mnemonic.strip().lower().split()
        if len(words) not in (12, 24):
            raise TonWalletError(f"seed-фраза должна быть из 24 (или 12) слов, а не {len(words)}")
        from tonutils.contracts import WalletV5R1

        try:
            WalletV5R1.validate_mnemonic(words)
        except Exception as e:  # noqa: BLE001
            raise TonWalletError(f"seed-фраза не прошла проверку: {e}") from e
        return words

    async def _get(self) -> Any:
        if self._wallet is not None:
            return self._wallet
        from ton_core import NetworkGlobalID
        from tonutils.clients import ToncenterClient
        from tonutils.contracts import WalletV4R2, WalletV5R1

        api_key = os.getenv("TONCENTER_API_KEY", "").strip() or None
        self._client = ToncenterClient(NetworkGlobalID.MAINNET, api_key=api_key, timeout=20)
        cls = WalletV4R2 if self._version == "v4r2" else WalletV5R1
        try:
            self._wallet, _, _, _ = cls.from_mnemonic(self._client, self._mnemonic.split())
        except Exception as e:  # noqa: BLE001
            raise TonWalletError(f"не удалось открыть кошелёк: {e}") from e
        return self._wallet

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
            self._wallet = None

    async def address(self) -> str:
        w = await self._get()
        return w.address.to_str(is_bounceable=False)

    async def balances(self) -> Balances:
        w = await self._get()
        from tonutils.contracts import JettonMasterStablecoinV2, JettonWalletStablecoinV2

        try:
            await w.refresh()
            ton = (w.balance or 0) / NANO
        except Exception as e:  # noqa: BLE001
            raise TonWalletError(f"не удалось получить баланс TON: {e}") from e
        usdt = 0.0
        try:
            master = await JettonMasterStablecoinV2.from_address(self._client, USDT_MASTER, load_state=False)
            jw_addr = await master.get_wallet_address(w.address)
            jw = await JettonWalletStablecoinV2.from_address(self._client, jw_addr)
            usdt = jw.jetton_balance / 10**USDT_DECIMALS
        except Exception as e:  # noqa: BLE001
            # Кошелёк USDT ещё не создан (ни разу не получал USDT) — баланс 0.
            log.info("USDT-кошелёк не прочитан (%s), считаем 0", e.__class__.__name__)
        return Balances(ton=ton, usdt=usdt)

    async def send_fragment_messages(self, messages: list[dict[str, Any]]) -> str:
        """Подписывает и отправляет сообщения TON Connect. Возвращает hash."""
        w = await self._get()
        from ton_core import Address, Cell, StateInit
        from tonutils.contracts import TONTransferBuilder

        builders = []
        for m in messages:
            try:
                dest = Address(str(m["address"]))
                amount = int(m.get("amount") or 0)
            except (KeyError, ValueError, TypeError) as e:
                raise TonWalletError(f"непонятное сообщение от Fragment: {m}") from e
            body = None
            if m.get("payload"):
                body = Cell.one_from_boc(base64.b64decode(m["payload"]))
            state_init = None
            if m.get("stateInit"):
                state_init = StateInit.deserialize(Cell.one_from_boc(base64.b64decode(m["stateInit"])).begin_parse())
            builders.append(TONTransferBuilder(destination=dest, amount=amount, body=body, state_init=state_init))
        try:
            if len(builders) == 1:
                ext = await w.transfer_message(builders[0])
            else:
                ext = await w.batch_transfer_message(builders)
        except Exception as e:  # noqa: BLE001
            raise TonWalletError(f"не удалось отправить транзакцию: {e}") from e
        return ext.normalized_hash
