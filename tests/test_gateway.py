from __future__ import annotations

import json

import pytest

from irl_gateway.gateway import FEATURE_SCHEMA_ID, TradeRequest
from irl_gateway.irl import IrlDenied, IrlUnavailable
from irl_gateway.journal import CONTEXT_PREFIX, context_hash

from .fakes import MODEL_HASH, FakeIrl, make_gateway


def _buy(**overrides) -> TradeRequest:
    fields = {
        "symbol": "BTC/USDT",
        "side": "buy",
        "rationale": "Trend up on the daily, volatility low; small starter position.",
        "quantity": 0.001,
    }
    fields.update(overrides)
    return TradeRequest(**fields)


def _journal_lines(tmp_path) -> list[dict]:
    return [json.loads(l) for l in (tmp_path / "journal.jsonl").read_text().splitlines()]


async def test_filled_trade_seals_rationale_hash_and_binds_the_fill(tmp_path):
    gateway, irl, broker = make_gateway(tmp_path)

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "filled"
    assert outcome.verdict == "MATCHED"
    assert outcome.trace_id == "trace-1"
    call = irl.authorize_calls[0]
    assert call["identity"].prompt_version == CONTEXT_PREFIX + outcome.context_sha256
    assert call["identity"].feature_schema_id == FEATURE_SCHEMA_ID
    assert call["identity"].model_hash_hex == MODEL_HASH
    assert call["is_buy"] is True
    assert call["asset"] == "BTC/USDT"
    assert call["venue_id"] == "paper-binance"
    assert call["notional"] == pytest.approx(50.0)
    assert call["notional_currency"] == "USDT"
    assert call["client_order_id"] == outcome.client_order_id
    assert len(outcome.client_order_id) <= 36

    bind = irl.bind_calls[0]
    assert bind["execution_status"] == "Filled"
    assert bind["executed_side"] == "Long"
    assert bind["executed_quantity"] == pytest.approx(0.001)
    assert (await broker.get_balances())["BTC"] == pytest.approx(0.001)


async def test_journal_lets_anyone_recompute_the_sealed_hash(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path)

    outcome = await gateway.execute_trade(_buy())

    intent, result = _journal_lines(tmp_path)
    assert intent["event"] == "intent" and result["event"] == "outcome"
    assert intent["context"]["rationale"].startswith("Trend up")
    recomputed = context_hash(intent["context"])
    assert recomputed == intent["context_sha256"] == outcome.context_sha256
    assert irl.authorize_calls[0]["identity"].prompt_version == CONTEXT_PREFIX + recomputed
    assert result["trace_id"] == "trace-1" and result["verdict"] == "MATCHED"


async def test_notional_is_converted_to_base_quantity_at_the_current_price(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path)

    outcome = await gateway.execute_trade(_buy(quantity=None, notional=100.0))

    assert outcome.status == "filled"
    assert irl.authorize_calls[0]["quantity"] == pytest.approx(0.002)


async def test_sell_is_sent_as_reduce_only_short_and_bound_as_short(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path, balances={"BTC": 0.01})

    outcome = await gateway.execute_trade(_buy(side="SELL", quantity=0.005))

    assert outcome.status == "filled"
    assert irl.authorize_calls[0]["is_buy"] is False
    assert irl.bind_calls[0]["executed_side"] == "Short"


async def test_policy_denial_sends_no_order(tmp_path):
    irl = FakeIrl(authorize_error=IrlDenied(403, "ASSET_UNAUTHORIZED", "Asset 'BTC/USDT' ..."))
    gateway, _, broker = make_gateway(tmp_path, irl)

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "denied"
    assert outcome.denial_code == "ASSET_UNAUTHORIZED"
    assert irl.bind_calls == []
    assert await broker.get_balances() == {"USDT": 1_000.0}
    assert _journal_lines(tmp_path)[-1]["status"] == "denied"


async def test_unauthorized_result_without_error_also_sends_no_order(tmp_path):
    gateway, _, broker = make_gateway(tmp_path, FakeIrl(authorized=False))

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "denied"
    assert outcome.trace_id == "trace-1"
    assert await broker.get_balances() == {"USDT": 1_000.0}


async def test_unreachable_irl_fails_closed(tmp_path):
    irl = FakeIrl(authorize_error=IrlUnavailable(0, "UNREACHABLE", "connection refused"))
    gateway, _, broker = make_gateway(tmp_path, irl)

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "blocked"
    assert "no order sent" in outcome.message
    assert await broker.get_balances() == {"USDT": 1_000.0}


async def test_venue_rejection_is_bound_as_rejected(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path, balances={"USDT": 1.0})  # can't afford 50 USDT

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "failed"
    assert "insufficient USDT" in outcome.message
    bind = irl.bind_calls[0]
    assert bind["execution_status"] == "Rejected"
    assert bind["exchange_tx_id"] == outcome.client_order_id


async def test_bind_failure_after_a_fill_still_reports_the_fill(tmp_path):
    irl = FakeIrl(bind_error=IrlUnavailable(503, "DB", "down"))
    gateway, _, broker = make_gateway(tmp_path, irl)

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "filled"
    assert outcome.bind_error and outcome.verdict is None
    assert any("reconcile" in w for w in outcome.warnings)
    assert (await broker.get_balances())["BTC"] == pytest.approx(0.001)


async def test_kill_switch_blocks_before_contacting_irl(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path)
    (tmp_path / "KILL").write_text("stop")

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "blocked"
    assert "kill switch" in outcome.message
    assert irl.authorize_calls == []


async def test_regime_ref_is_fetched_and_sent_when_configured(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path, use_regime_ref=True)

    await gateway.execute_trade(_buy())

    assert irl.regime_calls == 1
    assert irl.authorize_calls[0]["mta_ref"] == "ref-123"


async def test_price_failure_blocks_the_trade(tmp_path):
    async def broken(symbol: str) -> float:
        raise RuntimeError("exchange down")

    from irl_gateway.brokers import PaperBroker

    gateway, irl, _ = make_gateway(tmp_path, broker=PaperBroker(broken, {"USDT": 100.0}))

    outcome = await gateway.execute_trade(_buy())

    assert outcome.status == "blocked"
    assert irl.authorize_calls == []


@pytest.mark.parametrize(
    "overrides, fragment",
    [
        ({"side": "short"}, "side must be"),
        ({"symbol": "BTCUSDT"}, "BASE/QUOTE"),
        ({"quantity": None}, "exactly one"),
        ({"notional": 10.0}, "exactly one"),
        ({"quantity": 0.0}, "positive"),
        ({"quantity": -1.0}, "positive"),
        ({"rationale": "   "}, "rationale is required"),
        ({"rationale": "x" * 20_001}, "limited to"),
    ],
)
async def test_invalid_requests_are_blocked_without_contacting_irl(tmp_path, overrides, fragment):
    gateway, irl, _ = make_gateway(tmp_path)

    outcome = await gateway.execute_trade(_buy(**overrides))

    assert outcome.status == "blocked"
    assert fragment in outcome.message
    assert irl.authorize_calls == []
