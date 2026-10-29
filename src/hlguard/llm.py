from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from hlguard.errors import LLMError
from hlguard.models import Snapshot
from hlguard.redact import redact
from hlguard.strategy import propose

_LOG = logging.getLogger("hlguard.llm")

OFFLINE_INPUT_TOKENS = 128
GUARDED_OUTPUT_TOKENS = 48
GULLIBLE_OUTPUT_TOKENS = 64
GUARDED_MODEL = "holdfast-mock"
GULLIBLE_MODEL = "holdfast-gullible"
_DEFAULT_TIMEOUT = 60.0
_DEFAULT_RETRIES = 1
_ANTHROPIC_MAX_TOKENS = 1024
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_CAP_SECONDS = 30.0


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    model: str
    system: str
    latency_ms: float
    response_model: str = ""
    response_id: str = ""
    finish_reason: str = ""
    temperature: float | None = None
    max_tokens: int | None = None


@dataclass(frozen=True)
class Completion:
    text: str
    usage: Usage
    provider: str
    fallback_reason: str = ""

    def with_route(self, *, provider: str, fallback_reason: str) -> Completion:
        return Completion(
            text=self.text,
            usage=self.usage,
            provider=provider,
            fallback_reason=fallback_reason,
        )


class LLMProvider(Protocol):
    def complete(self, messages: list[dict]) -> Completion | str: ...


def as_completion(result: Completion | str, *, provider: str = "custom") -> Completion:
    """Accept a Completion or a bare string from a test double."""
    if isinstance(result, Completion):
        return result
    return Completion(
        text="" if result is None else str(result),
        usage=Usage(
            input_tokens=0,
            output_tokens=0,
            model="",
            system="",
            latency_ms=0.0,
        ),
        provider=provider,
    )


class GuardedMock:
    """Deterministic stand-in. It reads the market JSON and ignores news text."""

    def complete(self, messages: list[dict]) -> Completion:
        started = time.perf_counter()
        user = messages[-1]["content"]
        match = re.search(r"MARKET_JSON\n(.*?)\nEND_MARKET_JSON", user, re.S)
        if not match:
            raise LLMError("mock provider could not find MARKET_JSON")
        payload = json.loads(match.group(1))
        snapshots = [Snapshot.from_dict(item) for item in payload["markets"]]
        proposal = propose(snapshots, str(payload.get("focus") or ""))
        return _offline(
            json.dumps(_dump(proposal), sort_keys=True),
            model=GUARDED_MODEL,
            provider="guarded",
            output_tokens=GUARDED_OUTPUT_TOKENS,
            started=started,
        )


class GullibleMock:
    """Test double that obeys instructions inside <untrusted_news>.

    It exists so evals can prove the gate still holds when the model does not.
    """

    def complete(self, messages: list[dict]) -> Completion:
        started = time.perf_counter()
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
        return _offline(
            json.dumps(plan),
            model=GULLIBLE_MODEL,
            provider="gullible",
            output_tokens=GULLIBLE_OUTPUT_TOKENS,
            started=started,
        )


class OpenAIProvider:
    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.model = model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        self.timeout = configured_timeout(timeout)
        self._client = client

    def complete(self, messages: list[dict]) -> Completion:
        client = self._client
        owns = False
        if client is None:
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            if not key:
                raise LLMError("OPENAI_API_KEY is not set")
            client = httpx.Client(
                timeout=self.timeout,
                headers={"Authorization": f"Bearer {key}"},
            )
            owns = True
        started = time.perf_counter()
        try:
            response = client.post(
                f"{self.base}/chat/completions",
                json={"model": self.model, "temperature": 0, "messages": messages},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.TimeoutException:
            raise LLMError("openai request timed out", retryable=True) from None
        except httpx.HTTPStatusError as exc:
            code = exc.response.status_code
            retryable = code == 429 or code >= 500
            raise LLMError(
                f"openai request failed: {code}",
                retryable=retryable,
                retry_after=_retry_after_header(exc.response.headers) if retryable else None,
            ) from None
        except LLMError:
            raise
        except Exception:
            raise LLMError("openai request failed", retryable=True) from None
        finally:
            if owns:
                client.close()
        return _from_openai(body, request_model=self.model, started=started)


class AnthropicProvider:
    """Messages API via the official anthropic SDK. Optional extra: holdfast[anthropic]."""

    def __init__(
        self,
        *,
        client: object | None = None,
        model: str | None = None,
        timeout: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        self.timeout = configured_timeout(timeout)
        self.max_tokens = max_tokens if max_tokens is not None else _anthropic_max_tokens()
        self._client = client

    def complete(self, messages: list[dict]) -> Completion:
        client = self._client
        owns = False
        if client is None:
            key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not key:
                raise LLMError("ANTHROPIC_API_KEY is not set")
            client = _build_anthropic(key, self.timeout)
            owns = True
        started = time.perf_counter()
        try:
            return _complete_anthropic(
                client,
                messages,
                model=self.model,
                max_tokens=self.max_tokens,
                timeout=self.timeout,
                started=started,
            )
        finally:
            if owns:
                close = getattr(client, "close", None)
                if close is not None:
                    close()


class RouterProvider:
    """Try providers in order. Timeouts and failed requests fall through to the next one."""

    def __init__(
        self,
        providers: list[tuple[str, LLMProvider]] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
        sleep: Callable[[float], None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.timeout = configured_timeout(timeout)
        self.retries = configured_retries(retries)
        self._sleep = sleep or time.sleep
        self._rng = rng or random.Random()
        if providers is None:
            raw = os.environ.get("HLGUARD_LLM_PROVIDERS", "anthropic,openai")
            names = [part.strip().lower() for part in raw.split(",") if part.strip()]
            if not names:
                raise LLMError("HLGUARD_LLM_PROVIDERS is empty")
            for name in names:
                _reject_router_name(name)
            self.providers = [(name, build_provider(name, timeout=self.timeout)) for name in names]
        else:
            self.providers = list(providers)
            for name, _provider in self.providers:
                _reject_router_name(name)
        if not self.providers:
            raise LLMError("no providers configured")

    def complete(self, messages: list[dict]) -> Completion:
        errors: list[str] = []
        for name, provider in self.providers:
            attempt = 0
            while True:
                attempt += 1
                try:
                    result = as_completion(provider.complete(messages), provider=name)
                except LLMError as exc:
                    failure: LLMError = exc
                except (TimeoutError, httpx.TimeoutException):
                    failure = LLMError(f"{name} request timed out", retryable=True)
                except Exception:
                    failure = LLMError(f"{name} request failed")
                else:
                    return result.with_route(provider=name, fallback_reason="; ".join(errors))
                if failure.retryable and failure.retry_after is not None and failure.retry_after > _BACKOFF_CAP_SECONDS:
                    message = redact(str(failure))
                    reason = (
                        f"{name}: {message}; retry-after {failure.retry_after:g}s "
                        f"exceeds the {_BACKOFF_CAP_SECONDS:.0f}s cap"
                    )
                    _LOG.warning("provider %s skipped: %s", name, reason)
                    errors.append(reason)
                    break
                if failure.retryable and attempt <= self.retries:
                    self._sleep(backoff_delay(attempt, failure.retry_after, self._rng))
                    continue
                message = redact(str(failure))
                _LOG.warning("provider %s failed: %s", name, message)
                errors.append(f"{name}: {message}")
                break
        raise LLMError("all providers failed: " + "; ".join(errors))


def build_provider(name: str, *, timeout: float | None = None) -> LLMProvider:
    cleaned = name.strip().lower()
    if cleaned in {"mock", "guarded"}:
        return GuardedMock()
    if cleaned == "gullible":
        return GullibleMock()
    if cleaned == "openai":
        return OpenAIProvider(timeout=timeout)
    if cleaned == "anthropic":
        return AnthropicProvider(timeout=timeout)
    if cleaned == "router":
        return RouterProvider(timeout=timeout)
    raise LLMError(f"unknown LLM provider: {name}")


def _reject_router_name(name: str) -> None:
    if name == "router":
        raise LLMError("router cannot include itself")
    if name == "gullible":
        raise LLMError("router cannot include gullible")
    if name in {"mock", "guarded"} and os.environ.get("HLGUARD_ROUTER_ALLOW_MOCK") != "1":
        raise LLMError("router cannot include the offline mock unless HLGUARD_ROUTER_ALLOW_MOCK=1")


def backoff_delay(attempt: int, retry_after: float | None, rng: random.Random) -> float:
    """Seconds to wait before another try of the same provider.

    Exponential, with jitter, and at least a Retry-After that fits under the cap.
    The router does not call this when Retry-After is longer than the cap. It skips
    that provider instead of waiting a shortened interval.
    """
    exponent = max(0, attempt - 1)
    delay = min(_BACKOFF_CAP_SECONDS, _BACKOFF_BASE_SECONDS * (2**exponent))
    jittered = delay * (0.5 + rng.random() * 0.5)
    if retry_after is None:
        return jittered
    return min(_BACKOFF_CAP_SECONDS, max(jittered, float(retry_after)))


def _retry_after_header(headers: object) -> float | None:
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    raw = getter("retry-after")
    if raw is None or raw == "":
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(str(raw))
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            seconds = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds < 0:
        return 0.0
    return seconds


def configured_timeout(override: float | None, default: float = _DEFAULT_TIMEOUT) -> float:
    if override is not None:
        parsed = float(override)
        return parsed if parsed > 0 else default
    raw = os.environ.get("HLGUARD_LLM_TIMEOUT", "").strip()
    if not raw:
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return parsed if parsed > 0 else default


def configured_retries(override: int | None, default: int = _DEFAULT_RETRIES) -> int:
    if override is not None:
        return max(0, int(override))
    raw = os.environ.get("HLGUARD_LLM_RETRIES", "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _offline(text: str, *, model: str, provider: str, output_tokens: int, started: float) -> Completion:
    return Completion(
        text=text,
        usage=Usage(
            input_tokens=OFFLINE_INPUT_TOKENS,
            output_tokens=output_tokens,
            model=model,
            system="holdfast",
            latency_ms=_elapsed_ms(started),
            response_model=model,
            finish_reason="stop",
        ),
        provider=provider,
    )


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000.0)


def _tokens(value: object) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return max(0, number)


def _from_openai(body: object, *, request_model: str, started: float) -> Completion:
    if not isinstance(body, dict):
        raise LLMError("openai response missing content")
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LLMError("openai response missing content")
    message = choices[0].get("message") or {}
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or content == "":
        raise LLMError("openai response missing content")
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    response_model = str(body.get("model") or request_model)
    finish = str(choices[0].get("finish_reason") or "")
    response_id = str(body.get("id") or "")
    return Completion(
        text=content,
        usage=Usage(
            input_tokens=_tokens(usage.get("prompt_tokens") or usage.get("input_tokens")),
            output_tokens=_tokens(usage.get("completion_tokens") or usage.get("output_tokens")),
            model=request_model,
            system="openai",
            latency_ms=_elapsed_ms(started),
            response_model=response_model,
            response_id=response_id,
            finish_reason=finish,
            temperature=0.0,
        ),
        provider="openai",
    )


def _import_anthropic():
    try:
        import anthropic
    except ImportError:
        raise LLMError("anthropic package is not installed") from None
    return anthropic


def _build_anthropic(key: str, timeout: float):
    anthropic = _import_anthropic()
    kwargs: dict = {
        "api_key": key,
        "timeout": timeout,
        "max_retries": 0,
        "open_telemetry": False,
    }
    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    if base:
        kwargs["base_url"] = base
    return anthropic.Anthropic(**kwargs)


def _complete_anthropic(
    client,
    messages: list[dict],
    *,
    model: str,
    max_tokens: int,
    timeout: float,
    started: float,
) -> Completion:
    system, chat = _split_messages(messages)
    kwargs: dict = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": chat,
        "timeout": timeout,
    }
    if system:
        kwargs["system"] = system
    try:
        message = client.messages.create(**kwargs)
    except LLMError:
        raise
    except Exception as exc:
        raise _translate_anthropic(exc) from None
    text = _anthropic_text(message)
    usage = getattr(message, "usage", None)
    response_model = str(getattr(message, "model", "") or model)
    return Completion(
        text=text,
        usage=Usage(
            input_tokens=_tokens(getattr(usage, "input_tokens", 0)),
            output_tokens=_tokens(getattr(usage, "output_tokens", 0)),
            model=model,
            system="anthropic",
            latency_ms=_elapsed_ms(started),
            response_model=response_model,
            response_id=str(getattr(message, "id", "") or ""),
            finish_reason=str(getattr(message, "stop_reason", "") or ""),
            max_tokens=max_tokens,
        ),
        provider="anthropic",
    )


def _translate_anthropic(exc: Exception) -> LLMError:
    anthropic = _import_anthropic()
    if isinstance(exc, anthropic.APITimeoutError) or isinstance(exc, TimeoutError):
        return LLMError("anthropic request timed out", retryable=True)
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        retryable = status == 429 or status >= 500
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        return LLMError(
            f"anthropic request failed: {status}",
            retryable=retryable,
            retry_after=_retry_after_header(headers) if retryable else None,
        )
    if isinstance(exc, anthropic.APIConnectionError):
        return LLMError("anthropic request failed", retryable=True)
    return LLMError("anthropic request failed")


def _anthropic_text(message: object) -> str:
    parts: list[str] = []
    for block in getattr(message, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text:
            parts.append(text)
    if not parts:
        raise LLMError("anthropic response missing content")
    return "".join(parts)


def _split_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system_parts: list[str] = []
    chat: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        text = content if isinstance(content, str) else json.dumps(content)
        if role == "system":
            system_parts.append(text)
        else:
            if role not in {"user", "assistant"}:
                role = "user"
            chat.append({"role": role, "content": text})
    if not chat:
        chat.append({"role": "user", "content": ""})
    return "\n\n".join(system_parts), chat


def _anthropic_max_tokens() -> int:
    raw = os.environ.get("ANTHROPIC_MAX_TOKENS", "").strip()
    if not raw:
        return _ANTHROPIC_MAX_TOKENS
    try:
        value = int(raw)
    except ValueError:
        return _ANTHROPIC_MAX_TOKENS
    return value if value > 0 else _ANTHROPIC_MAX_TOKENS


def _dump(proposal) -> dict:
    if proposal.decision == "hold":
        return {"decision": "hold", "ticker": proposal.ticker, "rationale": proposal.rationale}
    canonical = proposal.canonical()
    canonical["decision"] = "trade"
    canonical["rationale"] = proposal.rationale
    return canonical
