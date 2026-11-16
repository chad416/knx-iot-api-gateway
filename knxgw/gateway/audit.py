"""Append-only audit log with a hash chain.

Every record carries the hash of the previous one, so a deleted or edited line is detected by
`verify`. Values of secrets and tokens are never written. This is tamper evidence, not tamper
proofing: someone who can rewrite the whole file can rebuild the chain. Shipping the last hash
to a second system would close that; it is listed as future work.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Optional


class AuditLog:
    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
        self.path, self.clock = path, clock
        self.records: list[dict] = []
        self._last = "0" * 64

    @staticmethod
    def _digest(rec: dict) -> str:
        body = {k: v for k, v in rec.items() if k != "hash"}
        return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def write(self, client: str, action: str, target: Any = None, result: str = "", **extra: Any) -> dict:
        rec = {"seq": len(self.records) + 1, "ts": round(self.clock(), 6), "client": client,
               "action": action, "target": target, "result": result, **extra, "prev": self._last}
        rec["hash"] = self._digest(rec)
        self._last = rec["hash"]
        self.records.append(rec)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        return rec

    @classmethod
    def verify(cls, records: list[dict]) -> tuple[bool, int]:
        """Returns (ok, index of the first bad record or -1)."""
        prev = "0" * 64
        for i, rec in enumerate(records):
            if rec.get("prev") != prev or rec.get("hash") != cls._digest(rec) or rec.get("seq") != i + 1:
                return False, i
            prev = rec["hash"]
        return True, -1
