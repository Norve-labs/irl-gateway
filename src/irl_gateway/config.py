"""Gateway settings from environment variables, validated at startup.

Required: IRL_BASE_URL, IRL_API_TOKEN, IRL_AGENT_ID, IRL_MODEL_HASH.
The broker defaults to paper trading at live public prices; a real account
needs GATEWAY_BROKER=exchange plus exchange credentials.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    irl_base_url: str
    irl_api_token: str
    agent_id: str
    model_hash_hex: str
    default_model_id: str
    hyperparameter_checksum: str
    use_regime_ref: bool
    broker: str  # "paper" | "exchange"
    exchange_id: str
    exchange_api_key: str
    exchange_api_secret: str
    exchange_testnet: bool
    paper_balances: dict[str, float]
    journal_path: Path
    kill_switch_path: Path


def load_settings(env: Mapping[str, str]) -> Settings:
    def required(name: str) -> str:
        value = env.get(name, "").strip()
        if not value:
            raise ConfigError(f"{name} is required")
        return value

    agent_id = required("IRL_AGENT_ID")
    if not _UUID.match(agent_id):
        raise ConfigError("IRL_AGENT_ID must be a UUID")
    model_hash = required("IRL_MODEL_HASH")
    if not _HEX64.match(model_hash):
        raise ConfigError(
            "IRL_MODEL_HASH must be 64 hex characters (the hash the agent was registered with)"
        )

    l2 = env.get("IRL_L2_MODE", "off").strip().lower()
    if l2 not in ("off", "regime"):
        raise ConfigError("IRL_L2_MODE must be 'off' or 'regime'")

    broker = env.get("GATEWAY_BROKER", "paper").strip().lower()
    if broker not in ("paper", "exchange"):
        raise ConfigError("GATEWAY_BROKER must be 'paper' or 'exchange'")
    exchange_id = env.get("EXCHANGE_ID", "binance").strip().lower()
    api_key = env.get("EXCHANGE_API_KEY", "").strip()
    api_secret = env.get("EXCHANGE_API_SECRET", "").strip()
    if broker == "exchange" and not (api_key and api_secret):
        raise ConfigError("GATEWAY_BROKER=exchange needs EXCHANGE_API_KEY and EXCHANGE_API_SECRET")

    home = Path(env.get("IRL_GATEWAY_HOME", str(Path.home() / ".irl-gateway")))
    return Settings(
        irl_base_url=required("IRL_BASE_URL"),
        irl_api_token=required("IRL_API_TOKEN"),
        agent_id=agent_id,
        model_hash_hex=model_hash.lower(),
        default_model_id=env.get("AGENT_MODEL_ID", "unspecified-model").strip()
        or "unspecified-model",
        hyperparameter_checksum=env.get("AGENT_CONFIG_CHECKSUM", "none").strip() or "none",
        use_regime_ref=l2 == "regime",
        broker=broker,
        exchange_id=exchange_id,
        exchange_api_key=api_key,
        exchange_api_secret=api_secret,
        exchange_testnet=_bool(env.get("EXCHANGE_TESTNET", "true")),
        paper_balances=parse_balances(env.get("PAPER_BALANCES", "USDT=1000")),
        journal_path=home / "journal.jsonl",
        kill_switch_path=home / "KILL",
    )


def parse_balances(text: str) -> dict[str, float]:
    balances: dict[str, float] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        asset, sep, amount = item.partition("=")
        try:
            value = float(amount)
        except ValueError:
            value = -1.0
        if not sep or not asset.strip() or value < 0:
            raise ConfigError(f"PAPER_BALANCES entries look like USDT=1000, got {item!r}")
        balances[asset.strip().upper()] = value
    return balances


def _bool(text: str) -> bool:
    value = text.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"expected a boolean, got {text!r}")
