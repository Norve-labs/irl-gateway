"""Local evidence journal: the agent's full rationale, kept next to IRL's seal.

IRL seals a hash, not the text. The gateway writes the canonical context
(rationale plus the trade inputs) here, puts its SHA-256 into the trace via
`prompt_version = "ctx-sha256:<hex>"`, and records what happened next. Anyone
holding a journal line can recompute the hash and match it to the sealed
trace; anyone holding only IRL's records learns nothing about the rationale.

Append-only JSON Lines: one event per line, never rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

CONTEXT_PREFIX = "ctx-sha256:"


def canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def context_hash(context: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(context).encode("utf-8")).hexdigest()


class Journal:
    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: str, client_order_id: str, **fields: Any) -> None:
        record = {
            "ts_ms": int(time.time() * 1000),
            "event": event,
            "client_order_id": client_order_id,
        }
        record.update(fields)
        line = json.dumps(record, sort_keys=True, ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent trades, newest first, one merged record per order."""
        if not self._path.exists():
            return []
        merged: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        with self._path.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                record = json.loads(raw)
                key = record["client_order_id"]
                if key not in merged:
                    merged[key] = {}
                    order.append(key)
                merged[key].update(record)
        return [merged[k] for k in reversed(order[-limit:])]
