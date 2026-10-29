from __future__ import annotations

import re

from fastapi.testclient import TestClient

from hlguard.agent import run_cycle
from hlguard.evalrunner import load_scenarios
from hlguard.llm import GuardedMock
from hlguard.metrics import content_type
from hlguard.models import NewsItem, Snapshot
from hlguard.web import create_app

_NAME = re.compile(r"^holdfast_[a-z0-9_]+$")


def test_snapshot_stays_counters_and_prometheus_text(deps) -> None:
    deps.metrics.inc("proposals")
    deps.metrics.inc("approvals", 2)
    deps.metrics.inc("rejections")
    deps.metrics.inc("policy_denials", 3)
    deps.metrics.inc("tool.list_markets", 4)
    deps.metrics.inc("tool.place_order")
    deps.metrics.inc('tool.say"hi')
    deps.metrics.observe("llm_call_latency_seconds", 0.02)
    deps.metrics.observe("llm_call_latency_seconds", 1.5)
    deps.metrics.observe("llm_call_latency_seconds", 1000.0)

    assert deps.metrics.snapshot() == {
        "approvals": 2,
        "policy_denials": 3,
        "proposals": 1,
        "rejections": 1,
        "tool.list_markets": 4,
        "tool.place_order": 1,
        'tool.say"hi': 1,
    }

    text = deps.metrics.render_prometheus()
    assert text.endswith("\n")
    assert "_histograms" not in text
    _assert_names(text)
    assert "holdfast_proposals_total 1\n" in text
    assert "holdfast_approvals_total 2\n" in text
    assert "holdfast_rejections_total 1\n" in text
    assert "holdfast_policy_denials_total 3\n" in text
    assert "holdfast_orders_total 0\n" in text
    assert 'holdfast_tool_calls_total{tool="list_markets"} 4\n' in text
    assert 'holdfast_tool_calls_total{tool="place_order"} 1\n' in text
    assert 'holdfast_tool_calls_total{tool="get_market"} 0\n' in text
    assert 'holdfast_tool_calls_total{tool="say\\"hi"} 1\n' in text
    assert "holdfast_tool_list_markets_total" not in text
    assert 'holdfast_llm_call_latency_seconds_bucket{le="0.01"} 0\n' in text
    assert 'holdfast_llm_call_latency_seconds_bucket{le="0.025"} 1\n' in text
    assert 'holdfast_llm_call_latency_seconds_bucket{le="1"} 1\n' in text
    assert 'holdfast_llm_call_latency_seconds_bucket{le="2.5"} 2\n' in text
    assert 'holdfast_llm_call_latency_seconds_bucket{le="120"} 2\n' in text
    assert 'holdfast_llm_call_latency_seconds_bucket{le="+Inf"} 3\n' in text
    assert "holdfast_llm_call_latency_seconds_count 3\n" in text
    assert "# TYPE holdfast_llm_call_latency_seconds histogram\n" in text
    assert "# TYPE holdfast_proposals_total counter\n" in text
    samples = _samples(text)
    buckets = [
        int(samples[f'holdfast_llm_call_latency_seconds_bucket{{le="{label}"}}'])
        for label in ("0.005", "0.01", "0.025", "1", "2.5", "120", "+Inf")
    ]
    assert buckets == sorted(buckets)


def test_metrics_endpoints(deps, book) -> None:
    client = TestClient(create_app(deps))
    empty = client.get("/metrics.json")
    assert empty.status_code == 200
    assert empty.json() == {}
    scraped = client.get("/metrics")
    assert scraped.status_code == 200
    assert scraped.headers["content-type"] == content_type()
    assert "holdfast_proposals_total 0\n" in scraped.text
    assert "holdfast_llm_call_latency_seconds_count 0\n" in scraped.text

    scenario = next(item for item in load_scenarios() if item["id"] == "eth_hold")
    record = run_cycle(
        deps,
        provider=GuardedMock(),
        snapshots=[Snapshot.from_dict(item) for item in book],
        news=[NewsItem(**item) for item in scenario["news"]],
        focus="ETH",
        llm_name="guarded",
    )
    assert record["status"] == "hold"
    assert "place_order" not in record["read_tools"]

    body = client.get("/metrics.json")
    assert body.status_code == 200
    assert body.headers["content-type"].startswith("application/json")
    payload = body.json()
    assert payload == deps.metrics.snapshot()
    assert payload["holds"] == 1
    assert payload.get("proposals", 0) == 0
    assert payload["tool.list_markets"] == 1
    assert payload["tool.get_news"] == 1
    assert payload["tool.get_account"] == 1
    assert "tool.place_order" not in payload
    assert "_histograms" not in payload

    text = client.get("/metrics")
    assert text.status_code == 200
    assert text.headers["content-type"] == content_type()
    assert "nosniff" in text.headers["x-content-type-options"]
    page = text.text
    _assert_names(page)
    assert "holdfast_holds_total 1\n" in page
    assert "holdfast_proposals_total 0\n" in page
    assert "holdfast_policy_denials_total 0\n" in page
    assert 'holdfast_tool_calls_total{tool="list_markets"} 1\n' in page
    assert 'holdfast_tool_calls_total{tool="get_news"} 1\n' in page
    assert 'holdfast_tool_calls_total{tool="get_account"} 1\n' in page
    assert 'holdfast_tool_calls_total{tool="place_order"} 0\n' in page
    assert "holdfast_llm_call_latency_seconds_count 1\n" in page
    assert 'holdfast_llm_call_latency_seconds_bucket{le="+Inf"} 1\n' in page
    assert page.endswith("\n")


def _assert_names(text: str) -> None:
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{", 1)[0].split(" ", 1)[0]
        assert _NAME.match(name), name
        assert "." not in name


def _samples(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        sample, _, value = line.rpartition(" ")
        found[sample] = value
    return found
