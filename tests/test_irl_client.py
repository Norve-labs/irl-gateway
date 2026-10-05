"""IrlClient against a real local HTTP server: payload shape and the error
mapping the gateway relies on to fail closed."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from irl_gateway.irl import AgentIdentity, IrlClient, IrlDenied, IrlError, IrlUnavailable

IDENTITY = AgentIdentity("agent", "a" * 64, "model", "ctx-sha256:abc", "irl-gateway/1", "none")


SEEN: dict[str, Any] = {}


async def _serve(handler, method: str, path: str) -> TestServer:
    SEEN.clear()
    app = web.Application()
    app.router.add_route(method, path, handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _json(status: int, body: dict[str, Any]):
    async def handler(request: web.Request) -> web.Response:
        SEEN["received"] = await request.json() if request.can_read_body else None
        SEEN["auth"] = request.headers.get("Authorization")
        return web.json_response(body, status=status)

    return handler


async def test_authorize_sends_sealed_identity_and_spot_fields():
    server = await _serve(
        _json(200, {"trace_id": "t1", "reasoning_hash": "h", "authorized": True}),
        "POST",
        "/irl/authorize",
    )
    client = IrlClient(str(server.make_url("")), "tok")
    try:
        result = await client.authorize(
            IDENTITY,
            is_buy=False,
            quantity=0.5,
            asset="BTC/USDT",
            notional=100.0,
            notional_currency="USDT",
            venue_id="binance",
            client_order_id="irl-1",
            mta_ref="ref",
        )
    finally:
        await client.close()
        await server.close()

    sent = SEEN["received"]
    assert result.trace_id == "t1" and result.authorized
    assert SEEN["auth"] == "Bearer tok"
    assert sent["prompt_version"] == "ctx-sha256:abc"
    assert sent["action"] == {"Short": 0.5}
    assert sent["reduce_only"] is True
    assert sent["order_type"] == "MARKET"
    assert sent["mta_ref"] == "ref"


async def test_bind_omits_unknown_optional_fields():
    server = await _serve(
        _json(200, {"trace_id": "t1", "verification_status": "MATCHED", "final_proof": "p"}),
        "POST",
        "/irl/bind-execution",
    )
    client = IrlClient(str(server.make_url("")), "tok")
    try:
        result = await client.bind("t1", exchange_tx_id="irl-1", execution_status="Rejected")
    finally:
        await client.close()
        await server.close()

    sent = SEEN["received"]
    assert result.verification_status == "MATCHED"
    assert set(sent) == {"trace_id", "exchange_tx_id", "execution_status", "execution_time_ms"}


@pytest.mark.parametrize(
    "status, exc_type",
    [(403, IrlDenied), (500, IrlUnavailable), (503, IrlUnavailable), (400, IrlError)],
)
async def test_http_errors_map_to_the_fail_closed_exceptions(status, exc_type):
    server = await _serve(
        _json(status, {"error": "SOME_CODE", "message": "nope"}), "GET", "/irl/trace/x"
    )
    client = IrlClient(str(server.make_url("")), "tok")
    try:
        with pytest.raises(exc_type) as info:
            await client.get_trace("x")
    finally:
        await client.close()
        await server.close()
    assert info.value.code == "SOME_CODE"
    assert info.value.status == status


async def test_unreachable_server_is_unavailable():
    client = IrlClient("http://127.0.0.1:9", "tok", timeout=2.0)
    try:
        with pytest.raises(IrlUnavailable) as info:
            await client.get_agent("a")
    finally:
        await client.close()
    assert info.value.code == "UNREACHABLE"


async def test_regime_without_ref_is_unavailable():
    server = await _serve(_json(200, {"regime_id": 0}), "GET", "/irl/regime")
    client = IrlClient(str(server.make_url("")), "tok")
    try:
        with pytest.raises(IrlUnavailable):
            await client.get_regime_ref()
    finally:
        await client.close()
        await server.close()


def test_client_requires_url_and_token():
    with pytest.raises(ValueError):
        IrlClient("", "tok")
    with pytest.raises(ValueError):
        IrlClient("http://irl", "")
