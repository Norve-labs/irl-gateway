"""Venues the gateway can execute on, behind one small async interface.

Symbols use ccxt's unified 'BASE/QUOTE' form (e.g. 'BTC/USDT'). Only spot
market orders are supported: the gateway's job is controlled, audited
execution, not order-type coverage.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

import ccxt.async_support as ccxt_async


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Fill:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    price: float
    fee: float
    fee_asset: str | None


class Broker(ABC):
    venue_id: str

    @abstractmethod
    async def get_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def get_balances(self) -> dict[str, float]:
        """Free balance per asset, non-zero only."""

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: Side, quantity: float, *, client_order_id: str
    ) -> Fill: ...

    @abstractmethod
    async def close(self) -> None: ...


def split_symbol(symbol: str) -> tuple[str, str]:
    base, sep, quote = symbol.partition("/")
    if not sep or not base or not quote:
        raise ValueError(f"symbol must look like 'BASE/QUOTE', got {symbol!r}")
    return base.upper(), quote.upper()


class CcxtBroker(Broker):
    """A real exchange account (or its testnet) through ccxt."""

    def __init__(self, exchange: Any):
        self._exchange = exchange
        self.venue_id = str(exchange.id)

    @classmethod
    def create(
        cls, exchange_id: str, api_key: str, api_secret: str, *, testnet: bool
    ) -> CcxtBroker:
        exchange_class = getattr(ccxt_async, exchange_id)
        exchange = exchange_class(
            {"apiKey": api_key, "secret": api_secret, "enableRateLimit": True}
        )
        if testnet:
            exchange.set_sandbox_mode(True)
        return cls(exchange)

    async def get_price(self, symbol: str) -> float:
        ticker = await self._exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    async def get_balances(self) -> dict[str, float]:
        balance = await self._exchange.fetch_balance()
        return {k: float(v) for k, v in balance.get("free", {}).items() if v}

    async def place_market_order(
        self, symbol: str, side: Side, quantity: float, *, client_order_id: str
    ) -> Fill:
        # clientOrderId is ccxt's unified param (newClientOrderId on Binance),
        # which links the exchange order to the sealed IRL intent.
        order = await self._exchange.create_order(
            symbol, "market", side.value, quantity, None, {"clientOrderId": client_order_id}
        )
        fee = order.get("fee") or {}
        return Fill(
            order_id=str(order.get("id", "")),
            symbol=symbol,
            side=side,
            quantity=float(order.get("filled") or quantity),
            price=float(order.get("average") or order.get("price") or 0.0),
            fee=float(fee.get("cost") or 0.0),
            fee_asset=fee.get("currency"),
        )

    async def close(self) -> None:
        await self._exchange.close()


PriceSource = Callable[[str], Awaitable[float]]


class PaperBroker(Broker):
    """Simulated fills at live prices with an in-memory balance sheet. No
    order ever leaves the process."""

    def __init__(
        self,
        price_source: PriceSource,
        balances: dict[str, float],
        *,
        venue_id: str = "paper",
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        on_close: Callable[[], Awaitable[None]] | None = None,
    ):
        self._price_source = price_source
        self._balances = {k.upper(): float(v) for k, v in balances.items()}
        self.venue_id = venue_id
        self._fee = fee_bps / 10_000
        self._slip = slippage_bps / 10_000
        self._ids = itertools.count(1)
        self._on_close = on_close

    async def get_price(self, symbol: str) -> float:
        return await self._price_source(symbol)

    async def get_balances(self) -> dict[str, float]:
        return {k: v for k, v in self._balances.items() if v}

    async def place_market_order(
        self, symbol: str, side: Side, quantity: float, *, client_order_id: str
    ) -> Fill:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        base, quote = split_symbol(symbol)
        mid = await self._price_source(symbol)
        price = mid * (1 + self._slip) if side is Side.BUY else mid * (1 - self._slip)
        gross = quantity * price
        fee = gross * self._fee
        if side is Side.BUY:
            self._debit(quote, gross + fee)
            self._credit(base, quantity)
        else:
            self._debit(base, quantity)
            self._credit(quote, gross - fee)
        return Fill(
            order_id=f"paper-{next(self._ids)}",
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=price,
            fee=fee,
            fee_asset=quote,
        )

    def _debit(self, asset: str, amount: float) -> None:
        available = self._balances.get(asset, 0.0)
        if amount > available + 1e-12:
            raise ValueError(f"insufficient {asset}: need {amount:.8f}, have {available:.8f}")
        self._balances[asset] = available - amount

    def _credit(self, asset: str, amount: float) -> None:
        self._balances[asset] = self._balances.get(asset, 0.0) + amount

    async def close(self) -> None:
        if self._on_close is not None:
            await self._on_close()


def public_price_source(
    exchange_id: str = "binance",
) -> tuple[PriceSource, Callable[[], Awaitable[None]]]:
    """Live last-trade prices from an exchange's public API (no keys)."""
    exchange = getattr(ccxt_async, exchange_id)({"enableRateLimit": True})

    async def price(symbol: str) -> float:
        ticker = await exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    return price, exchange.close
