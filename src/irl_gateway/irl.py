"""Async client for the IRL Engine REST API (the subset the gateway uses).

Error mapping is what lets the gateway fail closed:
  403           -> IrlDenied      (IRL evaluated the intent and said no)
  5xx / network -> IrlUnavailable (no decision could be made)
  other non-2xx -> IrlError       (bad request; a bug on our side)
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import aiohttp


class IrlError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"[{code}] {message} (HTTP {status})")
        self.status = status
        self.code = code
        self.message = message


class IrlDenied(IrlError):
    """IRL evaluated the intent and refused it."""


class IrlUnavailable(IrlError):
    """IRL could not be reached or could not make a decision."""


@dataclass(frozen=True)
class AgentIdentity:
    """Model-identity fields IRL seals into every reasoning trace."""

    agent_id: str
    model_hash_hex: str
    model_id: str
    prompt_version: str
    feature_schema_id: str
    hyperparameter_checksum: str


@dataclass(frozen=True)
class AuthorizeResult:
    trace_id: str
    reasoning_hash: str
    authorized: bool
    shadow_blocked: bool


@dataclass(frozen=True)
class BindResult:
    trace_id: str
    final_proof: str | None
    verification_status: str
    divergence_reason: str | None


class IrlClient:
    def __init__(self, base_url: str, token: str, *, timeout: float = 10.0):
        if not base_url or not token:
            raise ValueError("IRL base_url and token are required")
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def authorize(
        self,
        identity: AgentIdentity,
        *,
        is_buy: bool,
        quantity: float,
        asset: str,
        notional: float,
        notional_currency: str,
        venue_id: str,
        client_order_id: str,
        mta_ref: str | None = None,
    ) -> AuthorizeResult:
        # Spot only: a sell can only reduce a long, so it is flagged
        # reduce_only (IRL then lets exits through restrictive regimes).
        payload: dict[str, Any] = {
            "agent_id": identity.agent_id,
            "model_hash_hex": identity.model_hash_hex,
            "model_id": identity.model_id,
            "prompt_version": identity.prompt_version,
            "feature_schema_id": identity.feature_schema_id,
            "hyperparameter_checksum": identity.hyperparameter_checksum,
            "action": {"Long" if is_buy else "Short": quantity},
            "quantity": quantity,
            "asset": asset,
            "notional": notional,
            "notional_currency": notional_currency,
            "order_type": "MARKET",
            "venue_id": venue_id,
            "client_order_id": client_order_id,
            "reduce_only": not is_buy,
            "agent_valid_time": int(time.time() * 1000),
        }
        if mta_ref is not None:
            payload["mta_ref"] = mta_ref
        data = await self._request("POST", "/irl/authorize", payload)
        return AuthorizeResult(
            trace_id=str(data["trace_id"]),
            reasoning_hash=str(data["reasoning_hash"]),
            authorized=bool(data.get("authorized", False)),
            shadow_blocked=bool(data.get("shadow_blocked", False)),
        )

    async def bind(
        self,
        trace_id: str,
        *,
        exchange_tx_id: str,
        execution_status: str,
        execution_price: float | None = None,
        executed_quantity: float | None = None,
        executed_side: str | None = None,
        asset: str | None = None,
    ) -> BindResult:
        optional = {
            "execution_price": execution_price,
            "executed_quantity": executed_quantity,
            "executed_side": executed_side,
            "asset": asset,
        }
        payload = {
            "trace_id": trace_id,
            "exchange_tx_id": exchange_tx_id,
            "execution_status": execution_status,
            "execution_time_ms": int(time.time() * 1000),
            **{key: value for key, value in optional.items() if value is not None},
        }
        data = await self._request("POST", "/irl/bind-execution", payload)
        return BindResult(
            trace_id=str(data["trace_id"]),
            final_proof=data.get("final_proof"),
            verification_status=str(data["verification_status"]),
            divergence_reason=data.get("divergence_reason"),
        )

    async def get_regime_ref(self) -> str:
        """Layer 2 v2: IRL's current verified regime reference (`mta_ref`)."""
        data = await self._request("GET", "/irl/regime")
        mta_ref = data.get("mta_ref")
        if not isinstance(mta_ref, str) or not mta_ref:
            raise IrlUnavailable(0, "NO_MTA_REF", "GET /irl/regime returned no mta_ref")
        return mta_ref

    async def get_trace(self, trace_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/irl/trace/{trace_id}")

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/irl/agents/{agent_id}")

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(
        self, method: str, path: str, payload: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        if self._session is None:
            self._session = aiohttp.ClientSession(headers=self._headers, timeout=self._timeout)
        try:
            async with self._session.request(
                method, f"{self._base_url}{path}", json=payload
            ) as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise IrlUnavailable(0, "UNREACHABLE", f"{method} {path}: {exc!r}") from exc

        body = _parse_json(text)
        if 200 <= status < 300:
            return body
        code = str(body.get("error", "UNKNOWN"))
        message = str(body.get("message", text or "no response body"))
        if status == 403:
            raise IrlDenied(status, code, message)
        if status >= 500:
            raise IrlUnavailable(status, code, message)
        raise IrlError(status, code, message)


def _parse_json(text: str) -> dict[str, Any]:
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}
