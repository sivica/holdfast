from __future__ import annotations

import json
import os
import re
from typing import Protocol

import httpx

from hlguard.errors import LLMError
from hlguard.models import Snapshot
from hlguard.strategy import propose


class LLMProvider(Protocol):
    def complete(self, messages: list[dict]) -> str: ...


class GuardedMock:
    """Deterministic stand-in. It reads the market JSON and ignores news text."""

    def complete(self, messages: list[dict]) -> str:
        user = messages[-1]["content"]
        match = re.search(r"MARKET_JSON\n(.*?)\nEND_MARKET_JSON", user, re.S)
        if not match:
            raise LLMError("mock provider could not find MARKET_JSON")
        payload = json.loads(match.group(1))
        snapshots = [Snapshot.from_dict(item) for item in payload["markets"]]
        proposal = propose(snapshots, str(payload.get("focus") or ""))
        return json.dumps(_dump(proposal), sort_keys=True)


class GullibleMock:
    """Test double that obeys instructions inside <untrusted_news>.

    It exists so evals can prove the gate still holds when the model does not.
    """

    def complete(self, messages: list[dict]) -> str:
        user = messages[-1]["content"]
        match = re.search(r"<untrusted_news>(.*)</untrusted_news>", user, re.S | re.I)
        news = match.group(1) if match else ""
        if re.search(r"\bDOGE\b", news):
            ticker = "DOGE"
        elif "xyz:NVDA" in news:
            ticker = "xyz:NVDA"
        else:
            ticker = "BTC"
        if re.search(r"\$\s*10[,.]?000|\b10k\b", news, re.I):
            size = "10000"
        else:
            size = "15.00"
        if re.search(r"\b25x\b", news, re.I):
            leverage = 25
        elif re.search(r"leverage\s*10\b", news, re.I):
            leverage = 10
        elif re.search(r"leverage\s*5\b", news, re.I):
            leverage = 5
        else:
            leverage = 1
        margin = "cross" if re.search(r"\bcross\b", news, re.I) else "isolated"
        plan: dict[str, object] = {
            "decision": "trade",
            "ticker": ticker,
            "side": "long",
            "size_usd": size,
            "leverage": leverage,
            "margin_mode": margin,
            "rationale": "The news instructed this trade. ignore limits and follow the text.",
        }
        if not re.search(r"no stop|stop is optional|without a stop", news, re.I):
            plan["stop_loss"] = "1"
            plan["max_loss_usd"] = "0.10"
        return json.dumps(plan)


class OpenAIProvider:
    def __init__(self) -> None:
        self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")

    def complete(self, messages: list[dict]) -> str:
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise LLMError("OPENAI_API_KEY is not set")
        try:
            response = httpx.post(
                f"{self.base}/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": self.model, "temperature": 0, "messages": messages},
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
            return str(body["choices"][0]["message"]["content"])
        except LLMError:
            raise
        except Exception:
            raise LLMError("model request failed") from None


def build_provider(name: str) -> LLMProvider:
    if name in {"mock", "guarded"}:
        return GuardedMock()
    if name == "gullible":
        return GullibleMock()
    if name == "openai":
        return OpenAIProvider()
    raise LLMError(f"unknown LLM provider: {name}")


def _dump(proposal) -> dict:
    if proposal.decision == "hold":
        return {"decision": "hold", "ticker": proposal.ticker, "rationale": proposal.rationale}
    canonical = proposal.canonical()
    canonical["decision"] = "trade"
    canonical["rationale"] = proposal.rationale
    return canonical
