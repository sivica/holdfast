from __future__ import annotations

from tests.conftest import TEST_SECRET

from hlguard.market import write_market
from hlguard.mcp_client import execute, list_tool_names
from hlguard.models import Snapshot
from hlguard.runtime import child_env
from hlguard.strategy import propose


def test_child_env_is_least_privilege(deps, monkeypatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_API_WALLET_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("HLGUARD_ACCOUNT_ADDRESS", "0x" + "11" * 20)
    read_env = child_env("read", deps)
    trade_env = child_env("trade", deps)
    assert "HLGUARD_APPROVAL_SECRET" not in read_env
    assert "HYPERLIQUID_API_WALLET_KEY" not in read_env
    assert TEST_SECRET not in " ".join(read_env.values())
    assert trade_env["HLGUARD_APPROVAL_SECRET"] == TEST_SECRET
    assert "HYPERLIQUID_API_WALLET_KEY" not in trade_env
    assert "0x" + "ab" * 32 not in " ".join(trade_env.values())


def test_stdio_scopes_and_place_order(deps, book) -> None:
    write_market(deps.data_dir / "market.json", [Snapshot.from_dict(item) for item in book])
    assert list_tool_names(deps, "read") == ["get_account", "get_market", "get_news", "list_markets"]
    assert list_tool_names(deps, "trade") == ["place_order"]
    plan = propose([Snapshot.from_dict(item) for item in book], "BTC")
    missing = execute(deps, plan.order_args("not-a-token"))
    assert missing["ok"] is False
    token = deps.approvals.issue(plan.plan_hash())
    filled = execute(deps, plan.order_args(token))
    assert filled["ok"], filled
    assert filled["fill"]["venue"] == "paper"
    assert filled["fill"]["ticker"] == "BTC"
