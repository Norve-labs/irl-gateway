"""Venues the gateway can execute on, behind one small async interface.

Symbols use ccxt's unified 'BASE/QUOTE' form (e.g. 'BTC/USDT'). Only spot
market orders are supported: the gateway's job is controlled, audited
execution, not order-type coverage.

Every order quantity goes through ``normalize_quantity`` first: rounded down
to the venue's amount step and checked against its minimum amount and minimum
order value. The gateway does this before sealing, so the quantity IRL seals
is exactly the quantity sent. Paper trading applies the same public market
rules, so a paper run fails where a live one would.
"""

from __future__ import annotations

import json
import os
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import ccxt.async_support as ccxt_async
from ccxt.base.errors import BadSymbol, InvalidOrder


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


class OrderSizeError(ValueError):
    """The order can't be placed at this size on this venue (step, minimums)."""


QuantityRule = Callable[[str, float, float], Awaitable[float]]


async def ccxt_quantity_rule(exchange: Any, symbol: str, quantity: float, price: float) -> float:
    """Round ``quantity`` down to the market's amount step and enforce its
    minimum amount and minimum order value (cost). Raises OrderSizeError."""
    await exchange.load_markets()
    base, quote = split_symbol(symbol)
    try:
        market = exchange.market(symbol)
        rounded = float(exchange.amount_to_precision(symbol, quantity))
    except BadSymbol as exc:
        raise OrderSizeError(f"{symbol} is not traded on {exchange.id}") from exc
    except InvalidOrder as exc:
        raise OrderSizeError(
            f"{quantity:g} {base} is smaller than the smallest {symbol} order step on {exchange.id}"
        ) from exc
    limits = market.get("limits") or {}
    min_amount = (limits.get("amount") or {}).get("min")
    min_cost = (limits.get("cost") or {}).get("min")
    if rounded <= 0:
        raise OrderSizeError(f"{quantity:g} {base} rounds to zero on {exchange.id}")
    if min_amount and rounded < float(min_amount):
        raise OrderSizeError(
            f"{rounded:g} {base} is below the {exchange.id} minimum of {float(min_amount):g} {base}"
        )
    if min_cost and rounded * price < float(min_cost):
        raise OrderSizeError(
            f"order value {rounded * price:.2f} {quote} is below the {exchange.id} "
            f"minimum of {float(min_cost):g} {quote}"
        )
    return rounded


class Broker(ABC):
    venue_id: str

    async def normalize_quantity(self, symbol: str, quantity: float, price: float) -> float:
        """The quantity this venue will actually accept; OrderSizeError if none."""
        return quantity

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

    async def normalize_quantity(self, symbol: str, quantity: float, price: float) -> float:
        return await ccxt_quantity_rule(self._exchange, symbol, quantity, price)

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
    """Simulated fills at live prices. No order ever leaves the process.

    With ``state_path`` the balance sheet survives restarts: it is loaded from
    that file when present (``balances`` then only seeds a new account) and
    rewritten atomically after every fill. A state file that can't be read
    is an error, never a silent reset to the starting balances.
    """

    def __init__(
        self,
        price_source: PriceSource,
        balances: dict[str, float],
        *,
        venue_id: str = "paper",
        fee_bps: float = 10.0,
        slippage_bps: float = 5.0,
        on_close: Callable[[], Awaitable[None]] | None = None,
        state_path: str | os.PathLike[str] | None = None,
        quantity_rule: QuantityRule | None = None,
    ):
        self._price_source = price_source
        self._quantity_rule = quantity_rule
        self._state_path = Path(state_path) if state_path else None
        if self._state_path is not None and self._state_path.exists():
            self._balances = _load_balances(self._state_path)
        else:
            self._balances = {k.upper(): float(v) for k, v in balances.items()}
        self.venue_id = venue_id
        self._fee = fee_bps / 10_000
        self._slip = slippage_bps / 10_000
        self._on_close = on_close

    async def get_price(self, symbol: str) -> float:
        return await self._price_source(symbol)

    async def normalize_quantity(self, symbol: str, quantity: float, price: float) -> float:
        if self._quantity_rule is None:
            return quantity
        return await self._quantity_rule(symbol, quantity, price)

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
        self._save()
        return Fill(
            order_id=f"paper-{uuid.uuid4().hex[:16]}",
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

    def _save(self) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(self._state_path.name + ".tmp")
        tmp.write_text(json.dumps({"balances": self._balances}, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._state_path)


def _load_balances(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        balances = raw["balances"]
        parsed = {str(k).upper(): float(v) for k, v in balances.items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"paper state {path} is unreadable; fix or remove it: {exc}") from exc
    if any(v < 0 for v in parsed.values()):
        raise ValueError(f"paper state {path} has negative balances")
    return parsed


def public_price_source(
    exchange_id: str = "binance",
) -> tuple[PriceSource, Callable[[], Awaitable[None]]]:
    """Live last-trade prices from an exchange's public API (no keys)."""
    price, _, close = public_market(exchange_id)
    return price, close


def public_market(
    exchange_id: str = "binance",
) -> tuple[PriceSource, QuantityRule, Callable[[], Awaitable[None]]]:
    """Live prices and the venue's real order-size rules, from its public API
    (no keys): what paper trading needs to behave like the live venue."""
    exchange = getattr(ccxt_async, exchange_id)({"enableRateLimit": True})

    async def price(symbol: str) -> float:
        ticker = await exchange.fetch_ticker(symbol)
        return float(ticker["last"])

    async def rule(symbol: str, quantity: float, last_price: float) -> float:
        return await ccxt_quantity_rule(exchange, symbol, quantity, last_price)

    return price, rule, exchange.close
