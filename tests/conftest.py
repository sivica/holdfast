from __future__ import annotations

import json
from pathlib import Path

import pytest

from hlguard.config import find_root
from hlguard.runtime import build_deps
from hlguard.tracing import configure_tracing

TEST_SECRET = "test-approval-secret-value"
ROOT = find_root()


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HLGUARD_APPROVAL_SECRET", TEST_SECRET)
    monkeypatch.setenv("HLGUARD_MARKET_SOURCE", "file")
    monkeypatch.setenv("HLGUARD_VENUE", "paper")
    monkeypatch.delenv("HLGUARD_ALLOW_MAINNET", raising=False)
    monkeypatch.delenv("HYPERLIQUID_API_WALLET_KEY", raising=False)
    monkeypatch.delenv("HLGUARD_ACCOUNT_ADDRESS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)


@pytest.fixture
def deps(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    configure_tracing(data / "traces.jsonl")
    return build_deps(ROOT / "config" / "default.json", data)


@pytest.fixture
def book() -> list[dict]:
    payload = json.loads((ROOT / "evals" / "fixtures" / "desk.json").read_text())
    return payload["snapshots"]
