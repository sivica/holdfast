from __future__ import annotations

import json

from fastapi.testclient import TestClient
from tests.conftest import TEST_SECRET

from hlguard.agent import decide, run_cycle
from hlguard.evalrunner import load_scenarios
from hlguard.llm import GuardedMock
from hlguard.models import NewsItem, Snapshot
from hlguard.web import create_app


def _artifacts(deps) -> str:
    chunks = []
    for path in deps.data_dir.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(errors="ignore"))
    return "\n".join(chunks)


def test_audit_chain_detects_tampering(deps, book) -> None:
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
    assert deps.audit.verify()
    lines = deps.audit.path.read_text().splitlines()
    event = json.loads(lines[-1])
    event["ticker"] = "DOGE"
    lines[-1] = json.dumps(event)
    deps.audit.path.write_text("\n".join(lines) + "\n")
    assert deps.audit.verify() is False


def test_secrets_do_not_land_in_artifacts(deps, book, monkeypatch) -> None:
    key = "0x" + "ab" * 32
    monkeypatch.setenv("HYPERLIQUID_API_WALLET_KEY", key)
    monkeypatch.setenv("HLGUARD_ACCOUNT_ADDRESS", "0x" + "33" * 20)

    class Leaky:
        def complete(self, messages):
            return json.dumps(
                {
                    "decision": "hold",
                    "ticker": "ETH",
                    "rationale": (
                        "ETH mark 3240.50 versus prevDayPx 3234.00. Hourly funding is 0.00001. "
                        f"This is a hold. key={key} secret={TEST_SECRET}"
                    ),
                }
            )

    scenario = next(item for item in load_scenarios() if item["id"] == "eth_hold")
    record = run_cycle(
        deps,
        provider=Leaky(),
        snapshots=[Snapshot.from_dict(item) for item in book],
        news=[NewsItem(**item) for item in scenario["news"]],
        focus="ETH",
        llm_name="leaky",
    )
    assert "[REDACTED]" in record["rationale"]
    blob = _artifacts(deps)
    assert key not in blob
    assert TEST_SECRET not in blob
    assert "0x" + "33" * 20 not in blob


def test_cli_prompt_approves(deps, monkeypatch, capsys) -> None:
    from hlguard.cli import run

    monkeypatch.setattr("builtins.input", lambda _prompt: "approve")
    code = run(
        [
            "run",
            "--scenario",
            "btc_short_approved",
            "--data-dir",
            str(deps.data_dir),
            "--config",
            str(deps.config_path),
        ]
    )
    assert code == 0
    output = capsys.readouterr().out
    assert "Paper fill" in output
    assert "short BTC" in output
    assert TEST_SECRET not in output
    assert deps.audit.verify()


def test_web_approve_and_injection_drill(deps) -> None:
    client = TestClient(create_app(deps))
    page = client.get("/")
    assert page.status_code == 200
    assert "script-src 'none'" in page.headers["content-security-policy"]
    token = _token(page.text)
    blocked = client.post(
        "/cycle",
        data={"form_token": token, "scenario": "injection_notional"},
        follow_redirects=True,
    )
    assert blocked.status_code == 200
    assert "data-status='blocked'" in blocked.text
    assert "data-can-approve='false'" in blocked.text
    assert "notional_exceeds_max" in blocked.text
    assert "id='approve'" not in blocked.text
    denied = client.post(
        "/proposals/not-a-uuid/approve",
        data={"form_token": "nope"},
        follow_redirects=False,
    )
    assert denied.status_code == 303
    assert "bad_token" in denied.headers["location"]
    assert deps.broker.view()["positions"] == []

    token = _token(blocked.text)
    proposed = client.post(
        "/cycle",
        data={"form_token": token, "scenario": "btc_short_approved"},
        follow_redirects=True,
    )
    assert "data-status='pending'" in proposed.text
    assert "id='approve'" in proposed.text
    proposal_id = _proposal_id(deps)
    token = _token(proposed.text)
    filled = client.post(
        f"/proposals/{proposal_id}/approve",
        data={"form_token": token},
        follow_redirects=True,
    )
    assert "data-status='filled'" in filled.text
    assert "Paper fill" in filled.text or "PAPER FILL" in filled.text
    assert deps.broker.view()["positions"]
    health = client.get("/health").json()
    assert health["venue"] == "paper"
    assert TEST_SECRET not in json.dumps(health)


def test_reject_does_not_fill(deps, book) -> None:
    scenario = next(item for item in load_scenarios() if item["id"] == "sol_rejected_by_human")
    record = run_cycle(
        deps,
        provider=GuardedMock(),
        snapshots=[Snapshot.from_dict(item) for item in book],
        news=[NewsItem(**item) for item in scenario["news"]],
        focus="SOL",
        llm_name="guarded",
    )
    assert record["status"] == "pending"
    rejected = decide(deps, record["id"], "reject", "test")
    assert rejected["status"] == "rejected"
    assert rejected["fill"] is None
    try:
        decide(deps, record["id"], "approve", "test")
        approved = True
    except Exception:
        approved = False
    assert approved is False


def _token(html: str) -> str:
    marker = "name='form_token' value='"
    start = html.find(marker)
    assert start != -1
    start += len(marker)
    end = html.find("'", start)
    return html[start:end]


def _proposal_id(deps) -> str:
    from hlguard.runtime import list_proposals

    pending = [item for item in list_proposals(deps) if item["status"] == "pending"]
    assert pending
    return pending[-1]["id"]
