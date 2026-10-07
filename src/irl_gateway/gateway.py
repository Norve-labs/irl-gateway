"""The trade path every agent order takes: seal -> authorize -> place -> bind.

1. Validate the request and check the local kill switch.
2. Hash the agent's rationale plus the trade inputs, journal the plaintext,
   and seal the hash into the IRL trace (``prompt_version``).
3. Authorize with IRL. A denial, an unreachable IRL or a non-authorized result
   means no order is sent (fail closed).
4. Place the market order with the client order id IRL sealed, so the
   exchange order links back to the trace.
5. Bind the fill (or the rejection) to the trace; IRL returns MATCHED or
   DIVERGENT.

A bind failure after a real fill does not hide the fill: the outcome reports
it with ``bind_error`` so it can be reconciled (IRL lists it under
/irl/pending).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from irl_gateway.brokers import Broker, Fill, OrderSizeError, Side, split_symbol
from irl_gateway.irl import AgentIdentity, IrlClient, IrlDenied, IrlError
from irl_gateway.journal import CONTEXT_PREFIX, Journal, context_hash

logger = logging.getLogger(__name__)

FEATURE_SCHEMA_ID = "irl-gateway/1"
MAX_RATIONALE_CHARS = 20_000

Status = Literal["filled", "denied", "blocked", "failed"]


@dataclass(frozen=True)
class AgentConfig:
    agent_id: str
    model_hash_hex: str
    default_model_id: str
    hyperparameter_checksum: str = "none"
    use_regime_ref: bool = False


@dataclass(frozen=True)
class TradeRequest:
    symbol: str
    side: str
    rationale: str
    quantity: float | None = None
    notional: float | None = None
    model_id: str | None = None


@dataclass(frozen=True)
class TradeOutcome:
    status: Status
    message: str
    client_order_id: str
    context_sha256: str | None = None
    trace_id: str | None = None
    reasoning_hash: str | None = None
    verdict: str | None = None
    final_proof: str | None = None
    fill: dict[str, Any] | None = None
    denial_code: str | None = None
    bind_error: str | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [])}


class TradeGateway:
    def __init__(
        self,
        irl: IrlClient,
        broker: Broker,
        journal: Journal,
        agent: AgentConfig,
        *,
        kill_switch_path: str | Path | None = None,
    ):
        self._irl = irl
        self._broker = broker
        self._journal = journal
        self._agent = agent
        self._kill_switch = Path(kill_switch_path) if kill_switch_path else None

    @property
    def broker(self) -> Broker:
        return self._broker

    @property
    def irl(self) -> IrlClient:
        return self._irl

    @property
    def journal(self) -> Journal:
        return self._journal

    @property
    def agent(self) -> AgentConfig:
        return self._agent

    def kill_switch_engaged(self) -> bool:
        return self._kill_switch is not None and self._kill_switch.exists()

    async def execute_trade(self, request: TradeRequest) -> TradeOutcome:
        client_order_id = "irl-" + uuid.uuid4().hex  # Binance allows 36 chars
        problem = _validate(request)
        if problem:
            return TradeOutcome("blocked", problem, client_order_id)
        if self.kill_switch_engaged():
            return TradeOutcome(
                "blocked",
                f"kill switch engaged ({self._kill_switch}); delete the file to resume",
                client_order_id,
            )

        side = Side(request.side.lower())
        try:
            price = await self._broker.get_price(request.symbol)
        except Exception as exc:  # noqa: BLE001 - any venue error blocks the trade
            logger.warning("price lookup failed for %s: %r", request.symbol, exc)
            return TradeOutcome(
                "blocked", f"could not price {request.symbol}: {exc}", client_order_id
            )
        try:
            # Round to what the venue accepts BEFORE sealing, so the sealed
            # quantity is the one actually sent (otherwise bind is DIVERGENT).
            quantity = await self._broker.normalize_quantity(
                request.symbol, _base_quantity(request, price), price
            )
        except OrderSizeError as exc:
            return TradeOutcome("blocked", str(exc), client_order_id)
        except Exception as exc:  # noqa: BLE001 - venue metadata errors block the trade
            logger.warning("market rules lookup failed for %s: %r", request.symbol, exc)
            return TradeOutcome(
                "blocked", f"could not load {request.symbol} market rules: {exc}", client_order_id
            )
        notional = quantity * price
        _, quote = split_symbol(request.symbol)
        model_id = request.model_id or self._agent.default_model_id

        context = {
            "client_order_id": client_order_id,
            "model_id": model_id,
            "notional_estimate": round(notional, 8),
            "price_reference": price,
            "quantity": quantity,
            "rationale": request.rationale,
            "side": side.value,
            "symbol": request.symbol,
            "venue_id": self._broker.venue_id,
        }
        ctx_hash = context_hash(context)
        self._journal.append("intent", client_order_id, context=context, context_sha256=ctx_hash)

        outcome = await self._authorize_place_bind(
            client_order_id, ctx_hash, model_id, request.symbol, side, quantity, notional, quote
        )
        record = {k: v for k, v in outcome.to_dict().items() if k != "client_order_id"}
        self._journal.append("outcome", client_order_id, **record)
        return outcome

    async def _authorize_place_bind(
        self,
        client_order_id: str,
        ctx_hash: str,
        model_id: str,
        symbol: str,
        side: Side,
        quantity: float,
        notional: float,
        quote: str,
    ) -> TradeOutcome:
        identity = AgentIdentity(
            agent_id=self._agent.agent_id,
            model_hash_hex=self._agent.model_hash_hex,
            model_id=model_id,
            prompt_version=CONTEXT_PREFIX + ctx_hash,
            feature_schema_id=FEATURE_SCHEMA_ID,
            hyperparameter_checksum=self._agent.hyperparameter_checksum,
        )
        try:
            mta_ref = await self._irl.get_regime_ref() if self._agent.use_regime_ref else None
            auth = await self._irl.authorize(
                identity,
                is_buy=side is Side.BUY,
                quantity=quantity,
                asset=symbol,
                notional=notional,
                notional_currency=quote,
                venue_id=self._broker.venue_id,
                client_order_id=client_order_id,
                mta_ref=mta_ref,
            )
        except IrlDenied as exc:
            return TradeOutcome(
                "denied",
                f"IRL denied: {exc.message}",
                client_order_id,
                context_sha256=ctx_hash,
                denial_code=exc.code,
            )
        except IrlError as exc:
            return TradeOutcome(
                "blocked",
                f"IRL unavailable, no order sent: {exc}",
                client_order_id,
                context_sha256=ctx_hash,
            )

        trace = TradeOutcome(
            "denied",
            "",
            client_order_id,
            context_sha256=ctx_hash,
            trace_id=auth.trace_id,
            reasoning_hash=auth.reasoning_hash,
        )
        if not auth.authorized:
            return replace(trace, message="IRL did not authorize the intent")
        warnings = ["IRL shadow mode would have blocked this trade"] if auth.shadow_blocked else []

        try:
            fill = await self._broker.place_market_order(
                symbol, side, quantity, client_order_id=client_order_id
            )
        except Exception as exc:  # noqa: BLE001 - reported to the agent and bound as Rejected
            logger.warning("order %s failed at the venue: %r", client_order_id, exc)
            verdict, _, bind_error = await self._bind(auth.trace_id, client_order_id, None)
            return replace(
                trace,
                status="failed",
                message=f"venue rejected the order: {exc}",
                verdict=verdict,
                bind_error=bind_error,
                warnings=warnings,
            )

        verdict, final_proof, bind_error = await self._bind(auth.trace_id, client_order_id, fill)
        if bind_error:
            warnings.append("fill happened but the IRL bind failed; reconcile via /irl/pending")
        return replace(
            trace,
            status="filled",
            message=f"{side.value} {fill.quantity:g} {symbol} at {fill.price:g}",
            verdict=verdict,
            final_proof=final_proof,
            fill=_fill_dict(fill),
            bind_error=bind_error,
            warnings=warnings,
        )

    async def _bind(
        self, trace_id: str, client_order_id: str, fill: Fill | None
    ) -> tuple[str | None, str | None, str | None]:
        """Returns (verdict, final_proof, bind_error); never raises."""
        try:
            result = await self._irl.bind(
                trace_id,
                exchange_tx_id=(fill.order_id if fill and fill.order_id else client_order_id),
                execution_status="Filled" if fill else "Rejected",
                execution_price=fill.price if fill else None,
                executed_quantity=fill.quantity if fill else None,
                executed_side=("Long" if fill.side is Side.BUY else "Short") if fill else None,
                asset=fill.symbol if fill else None,
            )
        except IrlError as exc:
            logger.error("IRL bind failed for trace %s: %s", trace_id, exc)
            return None, None, str(exc)
        return result.verification_status, result.final_proof, None


def _validate(request: TradeRequest) -> str | None:
    if request.side.lower() not in (Side.BUY.value, Side.SELL.value):
        return "side must be 'buy' or 'sell'"
    try:
        split_symbol(request.symbol)
    except ValueError as exc:
        return str(exc)
    if (request.quantity is None) == (request.notional is None):
        return "give exactly one of quantity (base units) or notional (quote currency)"
    amount = request.quantity if request.quantity is not None else request.notional
    if amount is None or not amount > 0:
        return "quantity / notional must be a positive number"
    if not request.rationale.strip():
        return "rationale is required: explain why you are making this trade"
    if len(request.rationale) > MAX_RATIONALE_CHARS:
        return f"rationale is limited to {MAX_RATIONALE_CHARS} characters"
    return None


def _base_quantity(request: TradeRequest, price: float) -> float:
    if request.quantity is not None:
        return request.quantity
    if request.notional is None:  # excluded by _validate
        raise ValueError("quantity or notional is required")
    return request.notional / price


def _fill_dict(fill: Fill) -> dict[str, Any]:
    return {
        "order_id": fill.order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": fill.price,
        "fee": fill.fee,
        "fee_asset": fill.fee_asset,
    }
