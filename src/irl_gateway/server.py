"""MCP server exposing the gateway to any MCP-capable agent (Claude, ChatGPT,
custom). One tool moves money (`execute_trade`); the rest are read-only."""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from irl_gateway.brokers import CcxtBroker, PaperBroker, public_market
from irl_gateway.config import Settings, load_settings
from irl_gateway.gateway import AgentConfig, TradeGateway, TradeRequest
from irl_gateway.irl import IrlClient, IrlError
from irl_gateway.journal import Journal

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Trades placed through this server are checked and recorded by IRL before they
reach the exchange. Every execute_trade call needs a rationale: write down
what you saw and why you are trading. Its hash is sealed into a tamper-evident
audit trace, so be specific and honest. A 'denied' result means IRL's policy
refused the trade (for example the asset, venue or size is outside your
mandate); do not retry it with tweaks to get around the policy. Spot only:
'sell' can only sell an asset you hold."""


def build_gateway(settings: Settings) -> TradeGateway:
    if settings.broker == "exchange":
        broker: Any = CcxtBroker.create(
            settings.exchange_id,
            settings.exchange_api_key,
            settings.exchange_api_secret,
            testnet=settings.exchange_testnet,
        )
    else:
        price, quantity_rule, close = public_market(settings.exchange_id)
        broker = PaperBroker(
            price,
            settings.paper_balances,
            venue_id=f"paper-{settings.exchange_id}",
            on_close=close,
            state_path=settings.paper_state_path,
            quantity_rule=quantity_rule,
        )
    agent = AgentConfig(
        agent_id=settings.agent_id,
        model_hash_hex=settings.model_hash_hex,
        default_model_id=settings.default_model_id,
        hyperparameter_checksum=settings.hyperparameter_checksum,
        use_regime_ref=settings.use_regime_ref,
    )
    return TradeGateway(
        IrlClient(settings.irl_base_url, settings.irl_api_token),
        broker,
        Journal(settings.journal_path),
        agent,
        kill_switch_path=settings.kill_switch_path,
    )


def create_server(gateway: TradeGateway) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            await gateway.broker.close()
            await gateway.irl.close()

    server = MCPServer(name="irl-gateway", instructions=INSTRUCTIONS, lifespan=lifespan)

    @server.tool(
        annotations=ToolAnnotations(
            destructive_hint=True, idempotent_hint=False, open_world_hint=True
        )
    )
    async def execute_trade(
        symbol: str,
        side: str,
        rationale: str,
        quantity: float | None = None,
        notional: float | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Place a spot market order through IRL (authorize, place, bind).

        symbol: 'BASE/QUOTE', e.g. 'BTC/USDT'. side: 'buy' or 'sell'.
        Give exactly one of quantity (base units) or notional (quote amount).
        rationale: why you are trading; sealed into the audit trace.
        Returns status filled | denied | blocked | failed, plus the IRL
        trace_id and verdict (MATCHED / DIVERGENT) when an order was placed.
        """
        request = TradeRequest(
            symbol=symbol,
            side=side,
            rationale=rationale,
            quantity=quantity,
            notional=notional,
            model_id=model_id,
        )
        return (await gateway.execute_trade(request)).to_dict()

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def get_quote(symbol: str) -> dict[str, Any]:
        """Last traded price for a 'BASE/QUOTE' symbol on the gateway's venue."""
        return {"symbol": symbol, "price": await gateway.broker.get_price(symbol)}

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def get_balances() -> dict[str, Any]:
        """Free balances on the gateway's account (paper or exchange)."""
        return {
            "venue_id": gateway.broker.venue_id,
            "balances": await gateway.broker.get_balances(),
        }

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def get_policy() -> dict[str, Any]:
        """Your mandate as IRL enforces it: status, notional cap, allowed
        assets and venues. Check this before trading."""
        try:
            profile = await gateway.irl.get_agent(gateway.agent.agent_id)
        except IrlError as exc:
            return {"error": str(exc)}
        keys = ("status", "max_notional", "allowed_assets", "allowed_venues", "allowed_regimes")
        return {
            "agent_id": gateway.agent.agent_id,
            "venue_id": gateway.broker.venue_id,
            "kill_switch_engaged": gateway.kill_switch_engaged(),
            **{k: profile.get(k) for k in keys},
        }

    @server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
    async def get_trace(trace_id: str) -> dict[str, Any]:
        """The sealed IRL record for one trade: intent, verdict and proofs."""
        try:
            return await gateway.irl.get_trace(trace_id)
        except IrlError as exc:
            return {"error": str(exc)}

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    async def list_recent_trades(limit: int = 20) -> dict[str, Any]:
        """Your recent trades from the local journal, newest first, each with
        its rationale, context hash, IRL trace id and outcome."""
        limit = max(1, min(int(limit), 200))
        return {"journal": str(gateway.journal.path), "trades": gateway.journal.recent(limit)}

    return server


def main() -> None:
    if sys.argv[1:2] == ["init"]:  # `irl-gateway init`: onboarding, not the server
        from irl_gateway.init import main as init_main

        raise SystemExit(init_main(sys.argv[2:]))
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    gateway = build_gateway(load_settings(os.environ))
    create_server(gateway).run("stdio")
