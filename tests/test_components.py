"""Paper broker, journal and settings."""

from __future__ import annotations

import pytest

from irl_gateway.brokers import CcxtBroker, PaperBroker, Side, split_symbol
from irl_gateway.config import ConfigError, load_settings, parse_balances
from irl_gateway.journal import Journal, context_hash

from .fakes import AGENT_ID, MODEL_HASH, fixed_prices

# ---- paper broker ------------------------------------------------------------


async def test_paper_buy_and_sell_apply_slippage_and_fees():
    broker = PaperBroker(
        fixed_prices(BTC_USDT=100.0), {"usdt": 1_000.0}, fee_bps=10.0, slippage_bps=50.0
    )

    buy = await broker.place_market_order("BTC/USDT", Side.BUY, 2.0, client_order_id="a")
    sell = await broker.place_market_order("BTC/USDT", Side.SELL, 1.0, client_order_id="b")

    assert buy.price == pytest.approx(100.5) and sell.price == pytest.approx(99.5)
    balances = await broker.get_balances()
    assert balances["BTC"] == pytest.approx(1.0)
    expected_usdt = 1_000 - 201 * 1.001 + 99.5 * 0.999
    assert balances["USDT"] == pytest.approx(expected_usdt)
    assert buy.order_id != sell.order_id


async def test_paper_refuses_to_overspend_or_oversell():
    broker = PaperBroker(fixed_prices(BTC_USDT=100.0), {"USDT": 50.0})

    with pytest.raises(ValueError, match="insufficient USDT"):
        await broker.place_market_order("BTC/USDT", Side.BUY, 1.0, client_order_id="a")
    with pytest.raises(ValueError, match="insufficient BTC"):
        await broker.place_market_order("BTC/USDT", Side.SELL, 1.0, client_order_id="b")
    with pytest.raises(ValueError, match="positive"):
        await broker.place_market_order("BTC/USDT", Side.BUY, 0.0, client_order_id="c")
    assert await broker.get_balances() == {"USDT": 50.0}


@pytest.mark.parametrize("bad", ["BTCUSDT", "/USDT", "BTC/", ""])
def test_split_symbol_rejects_non_unified_symbols(bad):
    with pytest.raises(ValueError):
        split_symbol(bad)


class _FakeExchange:
    id = "binance"

    def __init__(self):
        self.orders = []

    async def fetch_ticker(self, symbol):
        return {"last": 123.0}

    async def fetch_balance(self):
        return {"free": {"USDT": 10.0, "BTC": 0.0}}

    async def create_order(self, symbol, kind, side, amount, price, params):
        self.orders.append((symbol, kind, side, amount, price, params))
        return {
            "id": 42,
            "filled": amount,
            "average": 101.0,
            "fee": {"cost": 0.1, "currency": "USDT"},
        }

    async def close(self):
        pass


async def test_ccxt_broker_sends_client_order_id_and_normalizes_the_fill():
    exchange = _FakeExchange()
    broker = CcxtBroker(exchange)

    fill = await broker.place_market_order("BTC/USDT", Side.BUY, 0.5, client_order_id="irl-x")

    assert exchange.orders[0][5] == {"clientOrderId": "irl-x"}
    assert exchange.orders[0][1:3] == ("market", "buy")
    assert (fill.order_id, fill.price, fill.fee, fill.fee_asset) == ("42", 101.0, 0.1, "USDT")
    assert await broker.get_price("BTC/USDT") == 123.0
    assert await broker.get_balances() == {"USDT": 10.0}
    assert broker.venue_id == "binance"


# ---- journal -----------------------------------------------------------------


def test_context_hash_ignores_key_order_but_not_content():
    assert context_hash({"a": 1, "b": "x"}) == context_hash({"b": "x", "a": 1})
    assert context_hash({"a": 1}) != context_hash({"a": 2})


def test_journal_merges_events_per_order_newest_first(tmp_path):
    journal = Journal(tmp_path / "sub" / "j.jsonl")
    journal.append("intent", "o1", context={"rationale": "first"})
    journal.append("intent", "o2", context={"rationale": "second"})
    journal.append("outcome", "o1", status="filled")

    recent = journal.recent()

    assert [r["client_order_id"] for r in recent] == ["o2", "o1"]
    assert recent[1]["status"] == "filled" and recent[1]["context"]["rationale"] == "first"
    assert journal.recent(limit=1)[0]["client_order_id"] == "o2"


def test_empty_journal_has_no_trades(tmp_path):
    assert Journal(tmp_path / "none.jsonl").recent() == []


# ---- settings ----------------------------------------------------------------

BASE_ENV = {
    "IRL_BASE_URL": "http://irl:4000",
    "IRL_API_TOKEN": "tok",
    "IRL_AGENT_ID": AGENT_ID,
    "IRL_MODEL_HASH": MODEL_HASH.upper(),
    "IRL_GATEWAY_HOME": "/tmp/gw",
}


def test_defaults_are_paper_trading_with_l2_off():
    settings = load_settings(BASE_ENV)

    assert settings.broker == "paper"
    assert settings.paper_balances == {"USDT": 1000.0}
    assert settings.use_regime_ref is False
    assert settings.model_hash_hex == MODEL_HASH
    assert settings.journal_path.name == "journal.jsonl"
    assert settings.kill_switch_path.name == "KILL"


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"IRL_API_TOKEN": ""}, "IRL_API_TOKEN is required"),
        ({"IRL_AGENT_ID": "not-a-uuid"}, "UUID"),
        ({"IRL_MODEL_HASH": "abc"}, "64 hex"),
        ({"IRL_L2_MODE": "v3"}, "IRL_L2_MODE"),
        ({"GATEWAY_BROKER": "live"}, "GATEWAY_BROKER"),
        ({"GATEWAY_BROKER": "exchange"}, "EXCHANGE_API_KEY"),
        ({"EXCHANGE_TESTNET": "maybe"}, "boolean"),
        ({"PAPER_BALANCES": "USDT"}, "PAPER_BALANCES"),
    ],
)
def test_invalid_settings_fail_fast(overrides, message):
    with pytest.raises(ConfigError, match=message):
        load_settings({**BASE_ENV, **overrides})


def test_exchange_broker_with_credentials_and_regime_mode():
    settings = load_settings(
        {
            **BASE_ENV,
            "GATEWAY_BROKER": "exchange",
            "EXCHANGE_API_KEY": "k",
            "EXCHANGE_API_SECRET": "s",
            "EXCHANGE_TESTNET": "false",
            "IRL_L2_MODE": "regime",
        }
    )
    assert settings.broker == "exchange" and settings.exchange_testnet is False
    assert settings.use_regime_ref is True


def test_parse_balances():
    assert parse_balances("usdt=100, btc=0.5") == {"USDT": 100.0, "BTC": 0.5}
    assert parse_balances("") == {}
    with pytest.raises(ConfigError):
        parse_balances("USDT=-1")


# ---- paper persistence -------------------------------------------------------


async def test_paper_account_survives_a_restart(tmp_path):
    state = tmp_path / "paper_state.json"
    first = PaperBroker(fixed_prices(BTC_USDT=100.0), {"USDT": 1_000.0}, state_path=state)
    await first.place_market_order("BTC/USDT", Side.BUY, 2.0, client_order_id="a")

    # The seed balances are ignored once a state file exists.
    second = PaperBroker(fixed_prices(BTC_USDT=100.0), {"USDT": 5.0}, state_path=state)

    assert await second.get_balances() == await first.get_balances()
    assert (await second.get_balances())["BTC"] == pytest.approx(2.0)


async def test_failed_order_does_not_touch_the_state_file(tmp_path):
    state = tmp_path / "paper_state.json"
    broker = PaperBroker(fixed_prices(BTC_USDT=100.0), {"USDT": 10.0}, state_path=state)

    with pytest.raises(ValueError):
        await broker.place_market_order("BTC/USDT", Side.BUY, 1.0, client_order_id="a")

    assert not state.exists()


@pytest.mark.parametrize(
    "content", ["not json", '{"nope": 1}', '{"balances": {"USDT": -5}}', '{"balances": [1]}']
)
def test_unreadable_paper_state_is_an_error_not_a_reset(tmp_path, content):
    state = tmp_path / "paper_state.json"
    state.write_text(content)

    with pytest.raises(ValueError, match="paper state"):
        PaperBroker(fixed_prices(BTC_USDT=1.0), {"USDT": 1_000.0}, state_path=state)
