"""Venue order-size rules: rounded before sealing, so sealed == sent."""

from __future__ import annotations

import ccxt.async_support as ccxt_async
import pytest

from irl_gateway.brokers import OrderSizeError, PaperBroker, ccxt_quantity_rule
from irl_gateway.gateway import TradeRequest

from .fakes import fixed_prices, make_gateway

# Binance-shaped spot market: 0.00001 BTC step, 5 USDT minimum order value.
BTC_USDT = {
    "id": "BTCUSDT",
    "symbol": "BTC/USDT",
    "base": "BTC",
    "quote": "USDT",
    "baseId": "BTC",
    "quoteId": "USDT",
    "type": "spot",
    "spot": True,
    "active": True,
    "precision": {"amount": 0.00001, "price": 0.01},
    "limits": {"amount": {"min": 0.00001, "max": 9000}, "cost": {"min": 5, "max": None}},
}


@pytest.fixture
async def binance():
    exchange = ccxt_async.binance()
    exchange.set_markets([BTC_USDT])  # offline: no network in tests
    yield exchange
    await exchange.close()


def rule_for(exchange):
    async def rule(symbol: str, quantity: float, price: float) -> float:
        return await ccxt_quantity_rule(exchange, symbol, quantity, price)

    return rule


async def test_quantity_is_rounded_down_to_the_step(binance):
    assert await ccxt_quantity_rule(binance, "BTC/USDT", 0.0123456789, 50_000.0) == 0.01234


@pytest.mark.parametrize(
    ("quantity", "fragment"),
    [
        (0.00005, "below the binance minimum of 5 USDT"),  # 2.50 USDT order
        (0.000001, "smallest BTC/USDT order step"),  # dust below the step
    ],
)
async def test_orders_the_venue_would_reject_raise(binance, quantity, fragment):
    with pytest.raises(OrderSizeError, match=fragment):
        await ccxt_quantity_rule(binance, "BTC/USDT", quantity, 50_000.0)


async def test_unknown_symbol_is_a_clear_error(binance):
    with pytest.raises(OrderSizeError, match="not traded on binance"):
        await ccxt_quantity_rule(binance, "FOO/BAR", 1.0, 1.0)


def _paper(binance) -> PaperBroker:
    return PaperBroker(
        fixed_prices(BTC_USDT=50_000.0),
        {"USDT": 1_000.0},
        venue_id="paper-binance",
        slippage_bps=0.0,
        quantity_rule=rule_for(binance),
    )


async def test_gateway_seals_and_sends_the_rounded_quantity(tmp_path, binance):
    gateway, irl, _ = make_gateway(tmp_path, broker=_paper(binance))

    outcome = await gateway.execute_trade(
        TradeRequest(symbol="BTC/USDT", side="buy", rationale="starter", notional=617.004)
    )

    # 617.004 / 50000 = 0.01234008 -> 0.01234; sealed and filled must agree.
    assert outcome.status == "filled", outcome.message
    assert irl.authorize_calls[0]["quantity"] == 0.01234
    assert outcome.fill["quantity"] == 0.01234
    assert irl.bind_calls[0]["executed_quantity"] == 0.01234


async def test_too_small_order_is_blocked_before_irl_is_asked(tmp_path, binance):
    gateway, irl, _ = make_gateway(tmp_path, broker=_paper(binance))

    outcome = await gateway.execute_trade(
        TradeRequest(symbol="BTC/USDT", side="buy", rationale="tiny", notional=2.0)
    )

    assert outcome.status == "blocked"
    assert "minimum of 5 USDT" in outcome.message
    assert irl.authorize_calls == []  # nothing sealed for an order that can't exist
