from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from hlguard.approval import FrozenClock
from hlguard.config import find_root
from hlguard.market import write_market
from hlguard.models import Snapshot
from hlguard.runtime import build_deps
from hlguard.strategy import propose
from hlguard.tools import place_order


def _seed(deps, book) -> None:
    write_market(deps.data_dir / "market.json", [Snapshot.from_dict(item) for item in book])


def _btc_plan(book):
    return propose([Snapshot.from_dict(item) for item in book], "BTC")


def test_token_is_bound_to_the_plan_hash(deps, book) -> None:
    _seed(deps, book)
    plan = _btc_plan(book)
    token = deps.approvals.issue(plan.plan_hash())
    ok = place_order(deps, plan.order_args(token))
    assert ok["ok"], ok
    assert ok["fill"]["leverage"] == 1
    assert ok["fill"]["margin_mode"] == "isolated"
    assert Decimal(ok["fill"]["notional_usd"]) <= Decimal("15.00")
    mutated = plan.order_args(token)
    mutated["size_usd"] = "10000.00"
    denied = place_order(deps, mutated)
    assert denied["ok"] is False
    assert denied["error"] == "plan_mismatch"


def test_missing_and_replayed_tokens(deps, book) -> None:
    _seed(deps, book)
    plan = _btc_plan(book)
    missing = place_order(deps, plan.order_args(""))
    assert missing["error"] == "malformed_token"
    token = deps.approvals.issue(plan.plan_hash())
    assert place_order(deps, plan.order_args(token))["ok"]
    replay = place_order(deps, plan.order_args(token))
    assert replay["error"] == "replay"
    assert len(deps.broker.view()["positions"]) == 1


def test_expired_token(book, tmp_path) -> None:
    clock = FrozenClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    deps = build_deps(find_root() / "config" / "default.json", tmp_path / "data", clock)
    _seed(deps, book)
    plan = _btc_plan(book)
    token = deps.approvals.issue(plan.plan_hash())
    clock.advance(601)
    denied = place_order(deps, plan.order_args(token))
    assert denied["error"] == "expired"


def test_signed_oversized_plan_still_refused(deps, book) -> None:
    _seed(deps, book)
    plan = _btc_plan(book)
    plan.size_usd = Decimal("10000")
    plan.leverage = 25
    plan.max_loss_usd = Decimal("100")
    plan.stop_loss = Decimal("70000")
    token = deps.approvals.issue(plan.plan_hash())
    denied = place_order(deps, plan.order_args(token))
    assert denied["ok"] is False
    assert "notional_exceeds_max" in denied["reasons"]
    assert "leverage_must_be_1" in denied["reasons"]
    assert deps.broker.view()["positions"] == []
