"""The MCP surface: tool list, safety annotations and structured results."""

from __future__ import annotations

from irl_gateway.brokers import PaperBroker
from irl_gateway.config import load_settings
from irl_gateway.server import build_gateway, create_server

from .fakes import AGENT_ID, MODEL_HASH, make_gateway


async def _call(server, name: str, args: dict | None = None) -> dict:
    result = await server.call_tool(name, args or {})
    assert not result.is_error, result.content
    return result.structured_content


async def test_only_execute_trade_is_destructive(tmp_path):
    gateway, _, _ = make_gateway(tmp_path)

    tools = {t.name: t for t in await create_server(gateway).list_tools()}

    assert set(tools) == {
        "execute_trade",
        "get_quote",
        "get_balances",
        "get_policy",
        "get_trace",
        "list_recent_trades",
    }
    assert tools["execute_trade"].annotations.destructive_hint is True
    assert tools["execute_trade"].input_schema["required"] == ["symbol", "side", "rationale"]
    for name, tool in tools.items():
        if name != "execute_trade":
            assert tool.annotations.read_only_hint is True, name


async def test_execute_trade_returns_the_structured_outcome(tmp_path):
    gateway, irl, _ = make_gateway(tmp_path)
    server = create_server(gateway)

    outcome = await _call(
        server,
        "execute_trade",
        {"symbol": "BTC/USDT", "side": "buy", "rationale": "test entry", "notional": 50.0},
    )

    assert outcome["status"] == "filled"
    assert outcome["verdict"] == "MATCHED"
    assert outcome["fill"]["quantity"] == 0.001
    assert len(irl.authorize_calls) == 1


async def test_read_tools(tmp_path):
    gateway, _, _ = make_gateway(tmp_path)
    server = create_server(gateway)
    await _call(
        server,
        "execute_trade",
        {"symbol": "BTC/USDT", "side": "buy", "rationale": "r", "quantity": 0.001},
    )
    (tmp_path / "KILL").write_text("")

    quote = await _call(server, "get_quote", {"symbol": "BTC/USDT"})
    balances = await _call(server, "get_balances")
    policy = await _call(server, "get_policy")
    trace = await _call(server, "get_trace", {"trace_id": "trace-1"})
    trades = await _call(server, "list_recent_trades", {"limit": 500})

    assert quote == {"symbol": "BTC/USDT", "price": 50_000.0}
    assert balances["venue_id"] == "paper-binance" and balances["balances"]["BTC"] == 0.001
    assert policy["max_notional"] == 200.0 and policy["allowed_assets"] == ["BTC/USDT"]
    assert policy["kill_switch_engaged"] is True
    assert "model_hash_hex" not in policy
    assert trace["verification_status"] == "MATCHED"
    assert trades["trades"][0]["status"] == "filled"
    assert trades["trades"][0]["context"]["rationale"] == "r"


async def test_build_gateway_defaults_to_paper_on_public_prices(tmp_path):
    settings = load_settings(
        {
            "IRL_BASE_URL": "http://irl:4000",
            "IRL_API_TOKEN": "tok",
            "IRL_AGENT_ID": AGENT_ID,
            "IRL_MODEL_HASH": MODEL_HASH,
            "IRL_GATEWAY_HOME": str(tmp_path),
            "PAPER_BALANCES": "USDT=250",
        }
    )

    gateway = build_gateway(settings)
    try:
        assert isinstance(gateway.broker, PaperBroker)
        assert gateway.broker.venue_id == "paper-binance"
        assert await gateway.broker.get_balances() == {"USDT": 250.0}
        assert gateway.journal.path == tmp_path / "journal.jsonl"
    finally:
        await gateway.broker.close()
        await gateway.irl.close()
