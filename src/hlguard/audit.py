from __future__ import annotations

import hashlib
import json
from pathlib import Path

from hlguard.approval import Clock
from hlguard.redact import redact

GENESIS = "0" * 64


def _canon(event: dict) -> str:
    body = {key: value for key, value in event.items() if key != "hash"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)


class AuditLog:
    def __init__(self, path: Path, clock: Clock):
        self.path = path
        self.clock = clock

    def append(self, event: dict) -> dict:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe = _redact_event(event)
        safe["ts"] = safe.get("ts") or self.clock.now().isoformat()
        from hlguard.store import locked

        with locked(self.path, "r+") as file:
            raw = file.read()
            lines = [line for line in raw.splitlines() if line.strip()]
            previous = json.loads(lines[-1])["hash"] if lines else GENESIS
            safe["prev"] = previous
            safe["hash"] = hashlib.sha256(_canon(safe).encode()).hexdigest()
            if raw and not raw.endswith("\n"):
                file.write("\n")
            file.write(json.dumps(safe, sort_keys=True) + "\n")
        return safe

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        lines = [line for line in self.path.read_text().splitlines() if line.strip()]
        return [json.loads(line) for line in lines]

    def verify(self) -> bool:
        previous = GENESIS
        for event in self.read():
            if event.get("prev") != previous:
                return False
            digest = hashlib.sha256(_canon(event).encode()).hexdigest()
            if event.get("hash") != digest:
                return False
            previous = event["hash"]
        return True


def _redact_event(event: dict) -> dict:
    def walk(value):
        if isinstance(value, str):
            return redact(value)
        if isinstance(value, list):
            return [walk(item) for item in value]
        if isinstance(value, dict):
            return {key: walk(item) for key, item in value.items()}
        return value

    return walk(dict(event))
