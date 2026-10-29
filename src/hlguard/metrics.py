from __future__ import annotations

from pathlib import Path

from hlguard.store import update_json


class Metrics:
    def __init__(self, path: Path):
        self.path = path

    def inc(self, name: str, amount: int = 1) -> None:
        def add(data: dict) -> dict:
            data[name] = int(data.get(name, 0)) + amount
            return data

        update_json(self.path, {}, add)

    def snapshot(self) -> dict[str, int]:
        from hlguard.store import read_json

        raw = read_json(self.path, {})
        return {key: int(value) for key, value in sorted(raw.items())}
