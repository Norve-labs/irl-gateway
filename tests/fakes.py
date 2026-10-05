"""In-memory stand-ins for IRL and the price feed."""

from __future__ import annotations

from typing import Any

from irl_gateway.brokers import PaperBroker
from irl_gateway.gateway import AgentConfig, TradeGateway
from irl_gateway.irl import AgentIdentity, AuthorizeResult, BindResult, IrlError
from irl_gateway.journal import Journal

AGENT_ID = "ff6cb58a-90ff-424e-a67f-fa6f626fe44b"
MODEL_HASH = "a" * 64


class FakeIrl:
    def __init__(
        self,
        *,
        authorize_error: IrlError | None = None,
        authorized: bool = True,
        bind_error: IrlError | None = None,
        verdict: str = "MATCHED",
    ):
        self.authorize_error = authorize_error
        self.authorized = authorized
        self.bind_error = bind_error
        self.verdict = verdict
        self.authorize_calls: list[dict[str, Any]] = []
        self.bind_calls: list[dict[str, Any]] = []
        self.regime_calls = 0
        self.closed = False

    async def authorize(self, identity: AgentIdentity, **kwargs: Any) -> AuthorizeResult:
        self.authorize_calls.append({"identity": identity, **kwargs})
        if self.authorize_error:
            raise self.authorize_error
        return AuthorizeResult(
            trace_id="trace-1",
            reasoning_hash="r" * 64,
            authorized=self.authorized,
            shadow_blocked=False,
        )

    async def bind(self, trace_id: str, **kwargs: Any) -> BindResult:
        self.bind_calls.append({"trace_id": trace_id, **kwargs})
        if self.bind_error:
            raise self.bind_error
        return BindResult(trace_id, "p" * 64, self.verdict, None)

    async def get_regime_ref(self) -> str:
        self.regime_calls += 1
        return "ref-123"

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return {"trace_id": trace_id, "verification_status": self.verdict}

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "status": "Active",
            "max_notional": 200.0,
            "allowed_assets": ["BTC/USDT"],
            "allowed_venues": None,
            "allowed_regimes": None,
            "model_hash_hex": MODEL_HASH,
        }

    async def close(self) -> None:
        self.closed = True


def fixed_prices(**prices: float):
    async def price(symbol: str) -> float:
        return prices[symbol.replace("/", "_")]

    return price


def make_gateway(
    tmp_path,
    irl: FakeIrl | None = None,
    *,
    balances: dict[str, float] | None = None,
    use_regime_ref: bool = False,
    broker: Any = None,
) -> tuple[TradeGateway, FakeIrl, Any]:
    irl = irl or FakeIrl()
    broker = broker or PaperBroker(
        fixed_prices(BTC_USDT=50_000.0),
        balances if balances is not None else {"USDT": 1_000.0},
        venue_id="paper-binance",
        fee_bps=10.0,
        slippage_bps=0.0,
    )
    gateway = TradeGateway(
        irl,  # type: ignore[arg-type]
        broker,
        Journal(tmp_path / "journal.jsonl"),
        AgentConfig(
            agent_id=AGENT_ID,
            model_hash_hex=MODEL_HASH,
            default_model_id="test-model",
            use_regime_ref=use_regime_ref,
        ),
        kill_switch_path=tmp_path / "KILL",
    )
    return gateway, irl, broker
