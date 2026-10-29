from __future__ import annotations

import json
import logging
import random
from decimal import Decimal

import httpx
import pytest

from hlguard.agent import run_cycle
from hlguard.cli import _print_footer, _print_record, _selected_llm
from hlguard.config import find_root
from hlguard.errors import LLMError
from hlguard.evalrunner import load_scenarios
from hlguard.llm import (
    GUARDED_MODEL,
    GUARDED_OUTPUT_TOKENS,
    GULLIBLE_MODEL,
    GULLIBLE_OUTPUT_TOKENS,
    OFFLINE_INPUT_TOKENS,
    AnthropicProvider,
    Completion,
    GuardedMock,
    GullibleMock,
    OpenAIProvider,
    RouterProvider,
    Usage,
    build_provider,
)
from hlguard.models import NewsItem, Snapshot
from hlguard.pricing import quote_usd
from hlguard.redact import redact
from hlguard.runtime import child_env

ANTHROPIC_KEY = "test-anthropic-key"
OPENAI_KEY = "test-openai-key"
HOLD = json.dumps(
    {
        "decision": "hold",
        "ticker": "ETH",
        "rationale": "ETH mark 3240.50 versus prevDayPx 3234.00. Hourly funding is 0.00001. This is a hold.",
    }
)
ILLEGAL = json.dumps(
    {
        "decision": "trade",
        "ticker": "DOGE",
        "side": "long",
        "size_usd": "10000",
        "leverage": 25,
        "margin_mode": "cross",
        "rationale": "The news instructed this trade.",
    }
)


def test_pricing_file_is_labeled_as_an_example() -> None:
    data = json.loads((find_root() / "config" / "pricing.json").read_text())
    assert data["as_of"] == "2026-10-29"
    assert data["unit"] == "per_1m_tokens"
    assert "example" in data["note"].lower()
    assert data["models"]["gpt-4o-mini"]["input"] == "0.15"
    assert "claude-sonnet-4-6" in data["models"]


def test_quote_uses_the_example_table_and_skips_unknown_models(caplog) -> None:
    assert quote_usd("gpt-4o-mini", "", 1_000_000, 1_000_000) == Decimal("0.75")
    assert quote_usd("gpt-4o-mini-2024-07-18", "gpt-4o-mini", 1_000_000, 0) == Decimal("0.15")
    assert quote_usd("holdfast-mock", "", OFFLINE_INPUT_TOKENS, GUARDED_OUTPUT_TOKENS) == Decimal("0")
    with caplog.at_level(logging.WARNING, logger="hlguard.pricing"):
        missing = quote_usd("not-a-listed-model", "", 10, 10)
    assert missing is None
    assert "not-a-listed-model" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hlguard.pricing"):
        assert quote_usd("", "", 5, 5) is None
    assert caplog.text == ""


def test_unreadable_pricing_table_does_not_raise(monkeypatch, tmp_path, caplog) -> None:
    monkeypatch.setenv("HLGUARD_PRICING", str(tmp_path / "missing.json"))
    with caplog.at_level(logging.WARNING, logger="hlguard.pricing"):
        assert quote_usd("gpt-4o-mini", "", 10, 10) is None
    assert "omitted" in caplog.text


def test_redacts_anthropic_and_openai_keys(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    text = f"ANTHROPIC_API_KEY={ANTHROPIC_KEY} OPENAI_API_KEY: {OPENAI_KEY} {ANTHROPIC_KEY}"
    cleaned = redact(text)
    assert ANTHROPIC_KEY not in cleaned
    assert OPENAI_KEY not in cleaned
    assert "[REDACTED]" in cleaned


def test_provider_keys_stay_out_of_mcp_env(deps, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_KEY)
    for scope in ("read", "trade"):
        env = child_env(scope, deps)
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert ANTHROPIC_KEY not in " ".join(env.values())
        assert OPENAI_KEY not in " ".join(env.values())


def test_build_provider_keeps_the_offline_default_and_names_the_router(monkeypatch) -> None:
    assert isinstance(build_provider("mock"), GuardedMock)
    assert isinstance(build_provider("guarded"), GuardedMock)
    assert isinstance(build_provider("gullible"), GullibleMock)
    monkeypatch.delenv("HLGUARD_LLM_PROVIDERS", raising=False)
    router = build_provider("router")
    assert isinstance(router, RouterProvider)
    assert [name for name, _provider in router.providers] == ["anthropic", "openai"]
    monkeypatch.setenv("HLGUARD_LLM_PROVIDERS", "openai, anthropic")
    ordered = build_provider("router")
    assert [name for name, _provider in ordered.providers] == ["openai", "anthropic"]
    monkeypatch.setenv("HLGUARD_LLM_PROVIDERS", "router")
    with pytest.raises(LLMError, match="itself"):
        build_provider("router")
    monkeypatch.setenv("HLGUARD_LLM_PROVIDERS", "gullible,openai")
    with pytest.raises(LLMError, match="gullible"):
        build_provider("router")
    monkeypatch.setenv("HLGUARD_LLM_PROVIDERS", "mock,openai")
    with pytest.raises(LLMError, match="offline mock"):
        build_provider("router")
    monkeypatch.setenv("HLGUARD_ROUTER_ALLOW_MOCK", "1")
    allowed = build_provider("router")
    assert [name for name, _provider in allowed.providers] == ["mock", "openai"]
    with pytest.raises(LLMError, match="unknown"):
        build_provider("bedrock")


def test_scenario_model_beats_the_env_and_the_flag_beats_both(monkeypatch) -> None:
    assert _selected_llm(None) == "mock"
    assert _selected_llm(None, "gullible") == "gullible"
    monkeypatch.setenv("HLGUARD_LLM", "router")
    assert _selected_llm(None, "gullible") == "gullible"
    assert _selected_llm("openai", "gullible") == "openai"
    assert _selected_llm(None) == "router"


def test_anthropic_requires_a_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        AnthropicProvider().complete([{"role": "user", "content": "hi"}])


def test_anthropic_missing_package(monkeypatch) -> None:
    import builtins

    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "anthropic" or name.startswith("anthropic."):
            raise ImportError("no anthropic")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ANTHROPIC_KEY)
    with pytest.raises(LLMError, match="not installed"):
        AnthropicProvider().complete([{"role": "user", "content": "hi"}])


def test_router_retries_transient_errors_and_skips_auth_failures() -> None:
    class Auth:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            raise LLMError("openai request failed: 401", retryable=False)

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("anthropic request timed out", retryable=True)
            return _completion(HOLD, model="claude-sonnet-4-6", system="anthropic", provider="anthropic")

    auth = Auth()
    flaky = Flaky()
    slept: list[float] = []
    router = RouterProvider(
        [("openai", auth), ("anthropic", flaky)],
        retries=1,
        timeout=1,
        sleep=slept.append,
        rng=random.Random(0),
    )
    result = router.complete([{"role": "user", "content": "hi"}])
    assert auth.calls == 1
    assert flaky.calls == 2
    assert result.provider == "anthropic"
    assert result.text == HOLD
    assert "401" in result.fallback_reason
    assert len(slept) == 1
    assert 0.25 <= slept[0] <= 0.5


def test_guarded_mock_records_fixed_usage(deps, capsys) -> None:
    record = _run(deps, GuardedMock(), "eth_hold", "guarded")
    assert record["status"] == "hold"
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["gen_ai.system"] == "holdfast"
    assert attrs["gen_ai.provider.name"] == "holdfast"
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.request.model"] == GUARDED_MODEL
    assert attrs["gen_ai.usage.input_tokens"] == OFFLINE_INPUT_TOKENS
    assert attrs["gen_ai.usage.output_tokens"] == GUARDED_OUTPUT_TOKENS
    assert attrs["holdfast.llm.provider"] == "guarded"
    assert attrs["holdfast.llm.fallback"] is False
    assert attrs["holdfast.cost_usd"] == 0
    assert attrs["holdfast.llm.provider_latency_ms"] >= 0
    snap = deps.metrics.snapshot()
    assert snap["llm_input_tokens"] == OFFLINE_INPUT_TOKENS
    assert snap["llm_output_tokens"] == GUARDED_OUTPUT_TOKENS
    assert snap["llm_cost_usd"] == 0
    assert "_histograms" not in snap
    assert record["served_by"] == "guarded"
    assert record["fallback_reason"] == ""
    assert record["input_tokens"] == OFFLINE_INPUT_TOKENS
    assert record["output_tokens"] == GUARDED_OUTPUT_TOKENS
    assert record["cost_usd"] == "0"
    _print_record(record)
    _print_footer(deps)
    output = capsys.readouterr().out
    assert "llm_cost_usd 0" in output
    assert "served by  guarded" in output
    assert "cost usd   0" in output
    assert "tokens     128 in / 48 out" in output
    assert "place_order" not in record["read_tools"]


def test_gullible_mock_records_usage_while_policy_blocks(deps) -> None:
    record = _run(deps, GullibleMock(), "injection_notional", "gullible")
    assert record["status"] == "blocked"
    assert record["token_issued"] is False
    assert "notional_exceeds_max" in record["reasons"]
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["gen_ai.request.model"] == GULLIBLE_MODEL
    assert attrs["gen_ai.usage.input_tokens"] == OFFLINE_INPUT_TOKENS
    assert attrs["gen_ai.usage.output_tokens"] == GULLIBLE_OUTPUT_TOKENS
    assert _span(deps, "holdfast.policy")["attributes"]["policy.ok"] is False
    assert deps.broker.view()["positions"] == []
    assert deps.metrics.snapshot().get("fills", 0) == 0
    assert deps.metrics.snapshot().get("tool.place_order", 0) == 0


def test_unknown_model_omits_cost_and_warns(deps, caplog) -> None:
    provider = _static(
        HOLD,
        model="not-a-listed-model",
        system="openai",
        provider="openai",
        input_tokens=10,
        output_tokens=4,
    )
    with caplog.at_level(logging.WARNING, logger="hlguard.pricing"):
        record = _run(deps, provider, "eth_hold", "openai")
    assert record["status"] == "hold"
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert "holdfast.cost_usd" not in attrs
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert record["cost_usd"] is None
    assert deps.metrics.snapshot()["llm_cost_usd"] == 0
    assert "not-a-listed-model" in caplog.text
    assert deps.broker.view()["positions"] == []


def test_known_model_cost_is_on_the_span_and_the_metric(deps) -> None:
    provider = _static(
        HOLD,
        model="gpt-4o-mini",
        system="openai",
        provider="openai",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        response_model="gpt-4o-mini-2024-07-18",
    )
    record = _run(deps, provider, "eth_hold", "openai")
    assert record["status"] == "hold"
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
    assert attrs["gen_ai.response.model"] == "gpt-4o-mini-2024-07-18"
    assert attrs["holdfast.cost_usd"] == pytest.approx(0.75)
    assert record["cost_usd"] == "0.75"
    assert record["input_tokens"] == 1_000_000
    assert deps.metrics.snapshot()["llm_cost_usd"] == pytest.approx(0.75)


def test_openai_mock_transport_sets_genai_attributes(deps, monkeypatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["host"] = request.url.host
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_openai_body(HOLD, prompt_tokens=1000, completion_tokens=500))

    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai.test/v1")
    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
    )
    try:
        record = _run(deps, OpenAIProvider(client=client, model="gpt-4o-mini", timeout=5), "eth_hold", "openai")
    finally:
        client.close()
    assert seen["host"] == "openai.test"
    assert seen["auth"] == f"Bearer {OPENAI_KEY}"
    assert seen["body"]["temperature"] == 0
    assert any(item["role"] == "system" for item in seen["body"]["messages"])
    assert record["status"] == "hold"
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-4o-mini"
    assert attrs["gen_ai.response.model"] == "gpt-4o-mini-2024-07-18"
    assert attrs["gen_ai.usage.input_tokens"] == 1000
    assert attrs["gen_ai.usage.output_tokens"] == 500
    assert attrs["gen_ai.response.finish_reasons"] == ["stop"]
    assert attrs["holdfast.llm.provider"] == "openai"
    assert attrs["holdfast.cost_usd"] == pytest.approx(0.00045)
    blob = (deps.data_dir / "traces.jsonl").read_text()
    assert OPENAI_KEY not in blob


def test_anthropic_mock_transport_sets_genai_attributes(deps) -> None:
    httpx2, anthropic = _anthropic_sdk()
    seen: dict = {}

    def handler(request) -> object:
        seen["host"] = request.url.host
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=_anthropic_body(HOLD))

    client = _anthropic_client(anthropic, httpx2, handler)
    try:
        record = _run(
            deps,
            AnthropicProvider(client=client, model="claude-sonnet-4-6", timeout=5),
            "eth_hold",
            "anthropic",
        )
    finally:
        client.close()
    assert seen["host"] == "anthropic.test"
    assert seen["path"] == "/v1/messages"
    assert seen["auth"] == f"Bearer {ANTHROPIC_KEY}"
    assert seen["body"]["model"] == "claude-sonnet-4-6"
    assert seen["body"]["max_tokens"] == 1024
    assert "system" in seen["body"]
    assert all(item["role"] != "system" for item in seen["body"]["messages"])
    assert record["status"] == "hold"
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["gen_ai.system"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-4-6"
    assert attrs["gen_ai.usage.input_tokens"] == 1000
    assert attrs["gen_ai.usage.output_tokens"] == 500
    assert attrs["gen_ai.response.id"] == "msg_01test"
    assert attrs["gen_ai.request.max_tokens"] == 1024
    assert attrs["holdfast.llm.provider"] == "anthropic"
    assert attrs["holdfast.llm.fallback"] is False
    assert attrs["holdfast.cost_usd"] == pytest.approx(0.0105)
    blob = (deps.data_dir / "traces.jsonl").read_text()
    assert ANTHROPIC_KEY not in blob


def test_router_429_falls_back_and_policy_still_blocks(deps, monkeypatch) -> None:
    httpx2, anthropic = _anthropic_sdk()
    calls = {"anthropic": 0, "openai": 0}

    def anthropic_handler(request) -> object:
        calls["anthropic"] += 1
        assert request.url.host == "anthropic.test"
        return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}})

    def openai_handler(request: httpx.Request) -> httpx.Response:
        calls["openai"] += 1
        assert request.url.host == "openai.test"
        return httpx.Response(200, json=_openai_body(ILLEGAL, prompt_tokens=100, completion_tokens=50))

    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai.test/v1")
    anthropic_client = _anthropic_client(anthropic, httpx2, anthropic_handler)
    openai_client = httpx.Client(
        transport=httpx.MockTransport(openai_handler),
        headers={"Authorization": f"Bearer {OPENAI_KEY}"},
    )
    router = RouterProvider(
        [
            ("anthropic", AnthropicProvider(client=anthropic_client, model="claude-sonnet-4-6", timeout=5)),
            ("openai", OpenAIProvider(client=openai_client, model="gpt-4o-mini", timeout=5)),
        ],
        retries=1,
        timeout=5,
        sleep=lambda _seconds: None,
    )
    try:
        record = _run(deps, router, "btc_short_approved", "router")
    finally:
        anthropic_client.close()
        openai_client.close()
    assert calls == {"anthropic": 2, "openai": 1}
    assert record["status"] == "blocked"
    assert record["token_issued"] is False
    assert "ticker_not_allowlisted" in record["reasons"]
    assert "notional_exceeds_max" in record["reasons"]
    assert "leverage_must_be_1" in record["reasons"]
    assert "place_order" not in record["read_tools"]
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["gen_ai.system"] == "openai"
    assert attrs["holdfast.llm.provider"] == "openai"
    assert record["served_by"] == "openai"
    assert record["llm"] == "router"
    assert "429" in record["fallback_reason"]
    assert attrs["holdfast.llm.fallback"] is True
    assert "429" in attrs["holdfast.llm.fallback_reason"]
    assert "anthropic" in attrs["holdfast.llm.fallback_reason"]
    assert _span(deps, "holdfast.policy")["attributes"]["policy.ok"] is False
    assert deps.broker.view()["positions"] == []
    assert deps.metrics.snapshot().get("tool.place_order", 0) == 0
    assert deps.metrics.snapshot().get("fills", 0) == 0
    blob = (deps.data_dir / "traces.jsonl").read_text()
    assert ANTHROPIC_KEY not in blob
    assert OPENAI_KEY not in blob


def test_router_timeout_falls_back_to_the_next_provider(monkeypatch) -> None:
    httpx2, anthropic = _anthropic_sdk()

    def anthropic_handler(request) -> object:
        raise httpx2.TimeoutException("timed out")

    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai.test/v1")

    def openai_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_openai_body(HOLD, prompt_tokens=10, completion_tokens=5))

    anthropic_client = _anthropic_client(anthropic, httpx2, anthropic_handler)
    openai_client = httpx.Client(transport=httpx.MockTransport(openai_handler))
    router = RouterProvider(
        [
            ("anthropic", AnthropicProvider(client=anthropic_client, model="claude-sonnet-4-6", timeout=5)),
            ("openai", OpenAIProvider(client=openai_client, model="gpt-4o-mini", timeout=5)),
        ],
        retries=0,
        timeout=5,
    )
    try:
        result = router.complete([{"role": "user", "content": "hi"}])
    finally:
        anthropic_client.close()
        openai_client.close()
    assert result.provider == "openai"
    assert result.usage.system == "openai"
    assert "timed out" in result.fallback_reason
    assert ANTHROPIC_KEY not in result.fallback_reason


def test_router_both_fail_places_no_order(deps, monkeypatch) -> None:
    httpx2, anthropic = _anthropic_sdk()
    calls = {"anthropic": 0, "openai": 0}

    def anthropic_handler(request) -> object:
        calls["anthropic"] += 1
        return httpx2.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}})

    def openai_handler(request: httpx.Request) -> httpx.Response:
        calls["openai"] += 1
        return httpx.Response(500, json={"error": {"message": "down"}})

    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai.test/v1")
    anthropic_client = _anthropic_client(anthropic, httpx2, anthropic_handler)
    openai_client = httpx.Client(transport=httpx.MockTransport(openai_handler))
    router = RouterProvider(
        [
            ("anthropic", AnthropicProvider(client=anthropic_client, model="claude-sonnet-4-6", timeout=5)),
            ("openai", OpenAIProvider(client=openai_client, model="gpt-4o-mini", timeout=5)),
        ],
        retries=0,
        timeout=5,
    )
    try:
        with pytest.raises(LLMError, match="all providers failed") as caught:
            _run(deps, router, "eth_hold", "router")
    finally:
        anthropic_client.close()
        openai_client.close()
    assert calls == {"anthropic": 1, "openai": 1}
    assert "429" in str(caught.value)
    assert "500" in str(caught.value)
    assert ANTHROPIC_KEY not in str(caught.value)
    assert OPENAI_KEY not in str(caught.value)
    assert _named(deps, "holdfast.policy") == []
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert "all providers failed" in attrs["holdfast.llm.error"]
    assert _span(deps, "holdfast.llm")["status"] == "ERROR"
    assert deps.broker.view()["positions"] == []
    assert deps.metrics.snapshot().get("fills", 0) == 0
    assert deps.metrics.snapshot().get("tool.place_order", 0) == 0


def test_router_fallback_to_guarded_mock_still_waits_for_a_human(deps, monkeypatch) -> None:
    class Down:
        def complete(self, messages):
            raise LLMError("anthropic request failed: 429", retryable=False)

    monkeypatch.setenv("HLGUARD_ROUTER_ALLOW_MOCK", "1")
    router = RouterProvider(
        [("anthropic", Down()), ("guarded", GuardedMock())],
        retries=0,
        timeout=1,
    )
    record = _run(deps, router, "btc_short_approved", "router")
    assert record["status"] == "pending"
    assert record["policy_ok"] is True
    assert record["token_issued"] is False
    assert record["plan"]["leverage"] == 1
    assert record["plan"]["size_usd"] == "10.00"
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["holdfast.llm.provider"] == "guarded"
    assert record["served_by"] == "guarded"
    assert "429" in record["fallback_reason"]
    events = [item for item in deps.audit.read() if item.get("event") == "proposal"]
    assert events[-1]["served_by"] == "guarded"
    assert "429" in events[-1]["fallback_reason"]
    assert attrs["gen_ai.system"] == "holdfast"
    assert attrs["holdfast.llm.fallback"] is True
    assert "429" in attrs["holdfast.llm.fallback_reason"]
    assert _span(deps, "holdfast.policy")["attributes"]["policy.ok"] is True
    assert deps.broker.view()["positions"] == []
    assert "place_order" not in record["read_tools"]


def test_retry_after_is_honored_without_sleeping() -> None:
    slept: list[float] = []

    class Rate:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("openai request failed: 429", retryable=True, retry_after=2.5)
            return _completion(HOLD, model="gpt-4o-mini", system="openai", provider="openai")

    rate = Rate()
    router = RouterProvider(
        [("openai", rate)],
        retries=1,
        timeout=1,
        sleep=slept.append,
        rng=random.Random(0),
    )
    result = router.complete([{"role": "user", "content": "hi"}])
    assert rate.calls == 2
    assert result.provider == "openai"
    assert len(slept) == 1
    assert slept[0] >= 2.5
    assert slept[0] <= 30


def test_openai_reports_retry_after(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "openai.test"
        return httpx.Response(429, headers={"retry-after": "4"}, json={"error": {"message": "slow"}})

    monkeypatch.setenv("OPENAI_BASE_URL", "http://openai.test/v1")
    client = httpx.Client(transport=httpx.MockTransport(handler), headers={"Authorization": f"Bearer {OPENAI_KEY}"})
    try:
        with pytest.raises(LLMError) as caught:
            OpenAIProvider(client=client, model="gpt-4o-mini", timeout=5).complete([{"role": "user", "content": "hi"}])
    finally:
        client.close()
    assert caught.value.retryable is True
    assert caught.value.retry_after == 4
    assert OPENAI_KEY not in str(caught.value)


def test_cycle_scrapes_metrics_json_and_prometheus(deps) -> None:
    from fastapi.testclient import TestClient

    from hlguard.metrics import content_type
    from hlguard.web import create_app

    record = _run(deps, GuardedMock(), "eth_hold", "guarded")
    assert record["status"] == "hold"
    assert record["cost_usd"] == "0"
    client = TestClient(create_app(deps))
    body = client.get("/metrics.json")
    assert body.status_code == 200
    assert body.headers["content-type"].startswith("application/json")
    payload = body.json()
    assert payload == deps.metrics.snapshot()
    assert "_histograms" not in payload
    assert payload["llm_input_tokens"] == OFFLINE_INPUT_TOKENS
    assert payload["llm_output_tokens"] == GUARDED_OUTPUT_TOKENS
    assert payload["llm_cost_usd"] == 0
    assert payload["holds"] == 1
    assert "tool.place_order" not in payload
    text = client.get("/metrics")
    assert text.status_code == 200
    assert text.headers["content-type"] == content_type()
    page = text.text
    assert "holdfast_llm_cost_usd_total 0\n" in page
    assert f"holdfast_llm_input_tokens_total {OFFLINE_INPUT_TOKENS}\n" in page
    assert f"holdfast_llm_output_tokens_total {GUARDED_OUTPUT_TOKENS}\n" in page
    assert "holdfast_holdfast_" not in page
    assert "holdfast_llm_call_latency_seconds_count 1\n" in page
    assert 'holdfast_llm_call_latency_seconds_bucket{le="+Inf"} 1\n' in page
    assert 'holdfast_tool_calls_total{tool="place_order"} 0\n' in page


def test_desk_shows_the_serving_provider(deps, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from hlguard.web import create_app

    class Down:
        def complete(self, messages):
            raise LLMError("anthropic request failed: 429", retryable=False)

    monkeypatch.setenv("HLGUARD_ROUTER_ALLOW_MOCK", "1")
    router = RouterProvider([("anthropic", Down()), ("guarded", GuardedMock())], retries=0, timeout=1)
    record = _run(deps, router, "btc_short_approved", "router")
    page = TestClient(create_app(deps)).get("/")
    assert page.status_code == 200
    assert "served by" in page.text
    assert record["served_by"] == "guarded"
    assert ">guarded<" in page.text
    assert "429" in page.text
    assert "place_order" not in record["read_tools"]


def test_live_refuses_gullible_from_the_flag(deps, capsys) -> None:
    from hlguard.cli import run

    code = run(
        [
            "run",
            "--live",
            "--llm",
            "gullible",
            "--data-dir",
            str(deps.data_dir),
            "--config",
            str(deps.config_path),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "gullible" in err
    assert "--live" in err
    assert deps.broker.view()["positions"] == []
    assert not (deps.data_dir / "proposals.json").exists()


def test_live_refuses_gullible_from_the_env(deps, monkeypatch, capsys) -> None:
    from hlguard.cli import run

    monkeypatch.setenv("HLGUARD_LLM", "gullible")
    code = run(
        [
            "run",
            "--live",
            "--data-dir",
            str(deps.data_dir),
            "--config",
            str(deps.config_path),
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "gullible" in err
    assert "--live" in err
    assert deps.broker.view()["positions"] == []
    assert not (deps.data_dir / "proposals.json").exists()


def test_retry_after_above_the_cap_skips_without_sleeping() -> None:
    slept: list[float] = []

    class Slow:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            raise LLMError("openai request failed: 429", retryable=True, retry_after=45)

    class Ok:
        def complete(self, messages):
            return _completion(HOLD, model="claude-sonnet-4-6", system="anthropic", provider="anthropic")

    slow = Slow()
    router = RouterProvider(
        [("openai", slow), ("anthropic", Ok())],
        retries=3,
        timeout=1,
        sleep=slept.append,
    )
    result = router.complete([{"role": "user", "content": "hi"}])
    assert slow.calls == 1
    assert slept == []
    assert result.provider == "anthropic"
    assert "45" in result.fallback_reason
    assert "exceeds the 30s cap" in result.fallback_reason

    alone = Slow()
    with pytest.raises(LLMError, match="exceeds the 30s cap") as caught:
        RouterProvider([("openai", alone)], retries=3, timeout=1, sleep=slept.append).complete(
            [{"role": "user", "content": "hi"}]
        )
    assert alone.calls == 1
    assert slept == []
    assert "45" in str(caught.value)


def test_histogram_times_the_whole_call_including_backoff(deps) -> None:
    import time

    class Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def complete(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise LLMError("openai request timed out", retryable=True)
            return _completion(
                HOLD,
                model="gpt-4o-mini",
                system="openai",
                provider="openai",
            )

    def pause(_seconds: float) -> None:
        time.sleep(0.05)

    flaky = Flaky()
    router = RouterProvider([("openai", flaky)], retries=1, timeout=1, sleep=pause)
    record = _run(deps, router, "eth_hold", "router")
    assert record["status"] == "hold"
    assert flaky.calls == 2
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["holdfast.llm.provider_latency_ms"] == pytest.approx(1.5)
    text = deps.metrics.render_prometheus()
    assert "holdfast_llm_call_latency_seconds_count 1\n" in text
    total = _histogram_sum(text)
    assert total >= 0.04
    assert total < 2


def test_histogram_times_a_non_llm_error(deps) -> None:
    class Boom:
        def complete(self, messages):
            raise RuntimeError("not an llm error")

    with pytest.raises(RuntimeError, match="not an llm error"):
        _run(deps, Boom(), "eth_hold", "boom")
    text = deps.metrics.render_prometheus()
    assert "holdfast_llm_call_latency_seconds_count 1\n" in text
    attrs = _span(deps, "holdfast.llm")["attributes"]
    assert attrs["holdfast.llm.error"] == "llm call failed"
    assert deps.broker.view()["positions"] == []
    assert deps.metrics.snapshot().get("tool.place_order", 0) == 0


def test_router_rejects_gullible_even_when_mock_is_allowed(monkeypatch) -> None:
    monkeypatch.setenv("HLGUARD_ROUTER_ALLOW_MOCK", "1")
    with pytest.raises(LLMError, match="gullible"):
        RouterProvider([("gullible", GullibleMock())], retries=0, timeout=1)


def _run(deps, provider, scenario_id: str, llm_name: str) -> dict:
    scenario = next(item for item in load_scenarios() if item["id"] == scenario_id)
    return run_cycle(
        deps,
        provider=provider,
        snapshots=[Snapshot.from_dict(item) for item in scenario["markets"]],
        news=[NewsItem(**item) for item in scenario["news"]],
        focus=scenario["focus"],
        llm_name=llm_name,
    )


def _histogram_sum(text: str) -> float:
    marker = "holdfast_llm_call_latency_seconds_sum "
    line = next(item for item in text.splitlines() if item.startswith(marker))
    return float(line.removeprefix(marker))


def _spans(deps) -> list[dict]:
    path = deps.data_dir / "traces.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _named(deps, name: str) -> list[dict]:
    return [item for item in _spans(deps) if item["name"] == name]


def _span(deps, name: str) -> dict:
    matches = _named(deps, name)
    assert matches, name
    return matches[-1]


def _completion(
    text: str,
    *,
    model: str,
    system: str,
    provider: str,
    input_tokens: int = 8,
    output_tokens: int = 4,
    response_model: str = "",
) -> Completion:
    return Completion(
        text=text,
        usage=Usage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            system=system,
            latency_ms=1.5,
            response_model=response_model or model,
            finish_reason="stop",
        ),
        provider=provider,
    )


def _static(
    text: str,
    *,
    model: str,
    system: str,
    provider: str,
    input_tokens: int,
    output_tokens: int,
    response_model: str = "",
):
    class Static:
        def complete(self, messages):
            return _completion(
                text,
                model=model,
                system=system,
                provider=provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                response_model=response_model,
            )

    return Static()


def _openai_body(content: str, *, prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "id": "chatcmpl-test",
        "model": "gpt-4o-mini-2024-07-18",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


def _anthropic_body(content: str) -> dict:
    return {
        "id": "msg_01test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }


def _anthropic_sdk():
    httpx2 = pytest.importorskip("httpx2")
    anthropic = pytest.importorskip("anthropic")
    return httpx2, anthropic


def _anthropic_client(anthropic, httpx2, handler):
    http = httpx2.Client(transport=httpx2.MockTransport(handler))
    return anthropic.Anthropic(
        api_key=ANTHROPIC_KEY,
        base_url="http://anthropic.test",
        http_client=http,
        max_retries=0,
        timeout=5.0,
        open_telemetry=False,
    )
