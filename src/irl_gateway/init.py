"""`irl-gateway init`: from nothing to a working MCP config in one command.

1. Get a free paper-tier token from the IRL server (POST /irl/signup).
2. Register an agent with a starter mandate (POST /irl/agents).
3. Save the credentials to ~/.irl-gateway/agent.json and print the MCP
   config to paste into Claude Code, Claude Desktop or any MCP client.

Paper-tier tokens can only trade on paper venues; ask the server's operator
for a full token to trade live.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shlex
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_SERVER = "https://norve.dev"

PostJson = Callable[[str, dict[str, Any], str | None], tuple[int, dict[str, Any]]]


class InitError(Exception):
    pass


def post_json(url: str, body: dict[str, Any], token: str | None) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json", "User-Agent": "irl-gateway-init"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return exc.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise InitError(f"could not reach {url}: {exc}") from exc


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="irl-gateway init",
        description="Get a free paper-trading token, register an agent, print the MCP config.",
    )
    p.add_argument(
        "--server", default=DEFAULT_SERVER, help=f"IRL server (default {DEFAULT_SERVER})"
    )
    p.add_argument("--name", default="my-agent", help="agent name (default my-agent)")
    p.add_argument("--contact", help="optional email or handle, so the operator can reach you")
    p.add_argument(
        "--max-notional",
        type=float,
        default=1000.0,
        help="per-order cap in quote currency (default 1000)",
    )
    p.add_argument("--assets", default="BTC/USDT,ETH/USDT", help="comma-separated allowed symbols")
    p.add_argument(
        "--exchange", default="binance", help="exchange whose prices and rules paper trading uses"
    )
    p.add_argument(
        "--model-id", default="claude-opus-5-5", help="model name sealed into each trace"
    )
    p.add_argument(
        "--home", default=str(Path.home() / ".irl-gateway"), help="where to save agent.json"
    )
    return p


def run_init(
    argv: list[str], *, post: PostJson = post_json, out: Callable[[str], None] = print
) -> int:
    args = _parser().parse_args(argv)
    server = args.server.rstrip("/")
    venue = f"paper-{args.exchange}"
    assets = [a.strip().upper() for a in args.assets.split(",") if a.strip()]
    if not assets or any("/" not in a for a in assets):
        raise InitError("--assets must look like BTC/USDT,ETH/USDT")
    if not args.max_notional > 0:
        raise InitError("--max-notional must be positive")

    signup_body: dict[str, Any] = {"client_name": args.name}
    if args.contact:
        signup_body["contact"] = args.contact
    status, body = post(f"{server}/irl/signup", signup_body, None)
    if status == 404 and body.get("error") == "SIGNUP_DISABLED":
        raise InitError(f"{server} doesn't offer self-serve signup; ask its operator for a token")
    if status != 201:
        raise InitError(f"signup failed (HTTP {status}): {body.get('message') or body}")
    token = body["token"]

    # A fresh identity for this agent. If you later change its model or
    # configuration in a way auditors should see, register a new agent.
    model_hash = hashlib.sha256(
        f"irl-gateway-init:{args.name}:{secrets.token_hex(16)}".encode()
    ).hexdigest()
    status, agent = post(
        f"{server}/irl/agents",
        {
            "name": args.name,
            "model_hash_hex": model_hash,
            "max_notional": args.max_notional,
            "allowed_assets": assets,
            "allowed_venues": [venue],
        },
        token,
    )
    if status != 201:
        raise InitError(
            f"agent registration failed (HTTP {status}): {agent.get('message') or agent}"
        )
    agent_id = agent["agent_id"]

    env = {
        "IRL_BASE_URL": server,
        "IRL_API_TOKEN": token,
        "IRL_AGENT_ID": agent_id,
        "IRL_MODEL_HASH": model_hash,
        "AGENT_MODEL_ID": args.model_id,
        "EXCHANGE_ID": args.exchange,
        "PAPER_BALANCES": "USDT=1000",
    }
    home = Path(args.home)
    home.mkdir(parents=True, exist_ok=True)
    saved = home / "agent.json"
    saved.write_text(json.dumps({"tier": body.get("tier"), "env": env}, indent=2), encoding="utf-8")
    try:
        os.chmod(saved, 0o600)
    except OSError:  # best effort on filesystems without POSIX modes
        pass

    mcp = {"mcpServers": {"irl-gateway": {"command": "uvx", "args": ["irl-gateway"], "env": env}}}
    claude_cmd = " ".join(
        ["claude", "mcp", "add", "irl-gateway"]
        + [f"-e {shlex.quote(f'{k}={v}')}" for k, v in env.items()]
        + ["--", "uvx", "irl-gateway"]
    )
    out(f"✓ Paper-tier token issued and agent '{args.name}' registered ({agent_id}).")
    out(f"  Mandate: {', '.join(assets)} on {venue}, at most {args.max_notional:g} per order.")
    out(f"  Saved to {saved} (keep it private: it holds your token).")
    out("")
    out("Claude Code, one command:")
    out(f"  {claude_cmd}")
    out("")
    out("Any MCP client (Claude Desktop, Cursor, ...), add to its config:")
    out(json.dumps(mcp, indent=2))
    out("")
    out("Then ask your agent to call get_policy, and make its first paper trade.")
    return 0


def main(argv: list[str]) -> int:
    try:
        return run_init(argv)
    except InitError as exc:
        print(f"irl-gateway init: {exc}")
        return 1
