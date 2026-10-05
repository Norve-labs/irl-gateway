# IRL Gateway

**Give your AI agent a trading account it can't misuse, and a record of every decision it can't rewrite.**

IRL Gateway is an [MCP](https://modelcontextprotocol.io) server that sits between an AI agent (Claude, ChatGPT, or your own) and an exchange account. Every order the agent places goes through the [IRL Engine](https://irl.macropulse.live):

1. **Policy before execution.** IRL checks the order against the agent's mandate (active status, notional cap, allowed assets and venues) before anything reaches the exchange. Out of mandate means no order.
2. **The rationale is sealed.** The agent must say why it is trading. The gateway hashes that rationale together with the trade inputs and seals the hash into IRL's tamper-evident trace, anchored daily to Bitcoin. The plaintext stays in your local journal.
3. **Intent is reconciled with the fill.** After the exchange fills the order, IRL compares what was authorized with what executed and records `MATCHED` or `DIVERGENT`.

When something goes wrong, you can prove what the agent was allowed to do, what it said it was doing, and what actually happened.

```
AI agent ── MCP ──> irl-gateway ──> IRL: authorize (policy + sealed rationale)
                         │
                         ├──────> exchange: market order (client id = sealed intent)
                         │
                         └──────> IRL: bind fill -> MATCHED / DIVERGENT
```

## Tools

| Tool | What it does |
| --- | --- |
| `execute_trade(symbol, side, rationale, quantity \| notional)` | The only tool that moves money. Spot market order through authorize → place → bind. Returns `filled`, `denied`, `blocked` or `failed`, with the IRL `trace_id` and verdict. |
| `get_policy()` | The agent's mandate as IRL enforces it, plus the local kill-switch state. |
| `get_quote(symbol)` | Last price on the gateway's venue. |
| `get_balances()` | Free balances (paper or exchange). |
| `get_trace(trace_id)` | IRL's sealed record of one trade. |
| `list_recent_trades(limit)` | Local journal: rationale, context hash, trace id and outcome per trade. |

Behaviour the agent can rely on:

- **Fail closed.** If IRL is unreachable or denies the intent, no order is sent.
- **Kill switch.** Create the file `~/.irl-gateway/KILL` and every trade is refused before IRL is even called. Delete it to resume.
- **No silent fills.** If the exchange fills but the IRL bind fails, the result still reports the fill and flags it for reconciliation.

## Quick start (paper trading)

You need an IRL server and an agent registered on it. Paper trading is the default: fills are simulated at live public Binance prices, and no exchange keys are needed.

```bash
pip install git+https://github.com/macropulse-lab/irl-gateway
```

Register the agent once, with its mandate:

```bash
curl -X POST "$IRL_BASE_URL/irl/agents" -H "Authorization: Bearer $IRL_API_TOKEN" \
  -H "Content-Type: application/json" -d '{
    "name": "my-claude-trader",
    "model_hash_hex": "<sha256 of your agent config>",
    "max_notional": 100,
    "allowed_assets": ["BTC/USDT", "ETH/USDT"],
    "allowed_venues": ["paper-binance"]
  }'
```

Then add the gateway to your MCP client, for example Claude Code or Claude Desktop:

```json
{
  "mcpServers": {
    "irl-gateway": {
      "command": "irl-gateway",
      "env": {
        "IRL_BASE_URL": "https://irl.example.com",
        "IRL_API_TOKEN": "…",
        "IRL_AGENT_ID": "<agent_id from registration>",
        "IRL_MODEL_HASH": "<the same model_hash_hex>",
        "AGENT_MODEL_ID": "claude-opus-5-5",
        "PAPER_BALANCES": "USDT=1000"
      }
    }
  }
}
```

Ask the agent to check `get_policy`, then trade.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `IRL_BASE_URL`, `IRL_API_TOKEN` | required | IRL server and bearer token |
| `IRL_AGENT_ID`, `IRL_MODEL_HASH` | required | The registered agent and its model hash |
| `AGENT_MODEL_ID` | `unspecified-model` | Model name sealed into each trace (the agent can override it per trade) |
| `AGENT_CONFIG_CHECKSUM` | `none` | Optional checksum of the agent's configuration, sealed into each trace |
| `IRL_L2_MODE` | `off` | `regime` if your IRL server requires Layer 2 regime binding |
| `GATEWAY_BROKER` | `paper` | `paper` or `exchange` |
| `EXCHANGE_ID` | `binance` | Any ccxt exchange id; also the price source for paper trading |
| `EXCHANGE_API_KEY`, `EXCHANGE_API_SECRET` | | Required for `exchange` |
| `EXCHANGE_TESTNET` | `true` | Use the exchange's testnet |
| `PAPER_BALANCES` | `USDT=1000` | Starting paper balances |
| `IRL_GATEWAY_HOME` | `~/.irl-gateway` | Journal (`journal.jsonl`) and kill switch (`KILL`) location |

The venue IRL sees is the exchange id (`binance`), or `paper-<exchange>` for paper trading, so a mandate can allow paper trading while denying the real account.

## How the rationale is sealed

For each trade the gateway builds a context of the rationale, symbol, side, quantity, reference price, venue, model id and client order id. It hashes that context as canonical JSON (sorted keys, no whitespace) with SHA-256 and sends the hash to IRL as `prompt_version = "ctx-sha256:<hex>"`, which IRL seals into the trace's `reasoning_hash`.

The journal stores the full context next to its hash, so anyone holding a journal line can recompute the hash and match it to the sealed trace. IRL itself never sees the rationale's text.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
pytest --cov=irl_gateway
ruff check src tests && black --check src tests && isort --check-only src tests && mypy src
```

## Status

Early (0.1). Spot market orders only. Paper trading and ccxt exchanges are supported; Alpaca is next. Not investment advice, and no strategy is included: the gateway controls and records what your agent does, it does not decide.

MIT licensed.
