"""`irl-gateway init`: signup -> register -> saved credentials + MCP config."""

from __future__ import annotations

import json

import pytest

from irl_gateway.init import InitError, run_init


class FakeServer:
    def __init__(self, signup=(201, None), register=(201, None)):
        self.calls: list[tuple[str, dict, str | None]] = []
        self._signup = signup
        self._register = register

    def __call__(self, url, body, token):
        self.calls.append((url, body, token))
        if url.endswith("/irl/signup"):
            status, payload = self._signup
            return status, payload or {"token": "tok-123", "token_id": "abc", "tier": "paper"}
        status, payload = self._register
        return status, payload or {"agent_id": "agent-1", "status": "Active"}


def test_init_signs_up_registers_and_prints_config(tmp_path):
    server = FakeServer()
    lines: list[str] = []

    rc = run_init(
        [
            "--server",
            "https://irl.test/",
            "--name",
            "bot",
            "--contact",
            "me@x.io",
            "--home",
            str(tmp_path),
        ],
        post=server,
        out=lines.append,
    )

    assert rc == 0
    (signup_url, signup_body, signup_token), (reg_url, reg_body, reg_token) = server.calls
    assert signup_url == "https://irl.test/irl/signup" and signup_token is None
    assert signup_body == {"client_name": "bot", "contact": "me@x.io"}
    assert reg_url == "https://irl.test/irl/agents" and reg_token == "tok-123"
    assert reg_body["allowed_venues"] == ["paper-binance"]
    assert reg_body["allowed_assets"] == ["BTC/USDT", "ETH/USDT"]
    assert len(reg_body["model_hash_hex"]) == 64

    saved = json.loads((tmp_path / "agent.json").read_text())
    assert saved["env"]["IRL_API_TOKEN"] == "tok-123"
    assert saved["env"]["IRL_AGENT_ID"] == "agent-1"
    assert saved["env"]["IRL_MODEL_HASH"] == reg_body["model_hash_hex"]
    text = "\n".join(lines)
    assert "claude mcp add irl-gateway" in text
    assert '"IRL_BASE_URL": "https://irl.test"' in text


def test_init_explains_a_server_without_signup(tmp_path):
    server = FakeServer(signup=(404, {"error": "SIGNUP_DISABLED"}))
    with pytest.raises(InitError, match="doesn't offer self-serve signup"):
        run_init(["--home", str(tmp_path)], post=server, out=lambda _: None)
    assert len(server.calls) == 1  # never tries to register


def test_init_surfaces_the_quota_message(tmp_path):
    server = FakeServer(signup=(429, {"error": "QUOTA_EXCEEDED", "message": "try again tomorrow"}))
    with pytest.raises(InitError, match="try again tomorrow"):
        run_init(["--home", str(tmp_path)], post=server, out=lambda _: None)


def test_init_rejects_malformed_assets(tmp_path):
    with pytest.raises(InitError, match="--assets"):
        run_init(
            ["--assets", "BTCUSDT", "--home", str(tmp_path)], post=FakeServer(), out=lambda _: None
        )
