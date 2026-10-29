from __future__ import annotations

import json
from decimal import Decimal

from hlguard.config import find_root, load_settings
from hlguard.models import REQUIRED_LEVERAGE, REQUIRED_MARGIN, Proposal, Snapshot
from hlguard.policy import evaluate
from hlguard.strategy import propose


def _account() -> dict:
    return {"positions": [], "realized_pnl_today": "0.00"}


def _btc(book: list[dict]) -> Snapshot:
    return next(Snapshot.from_dict(item) for item in book if item["ticker"] == "BTC")


def test_repo_defaults_are_paper() -> None:
    raw = json.loads((find_root() / "config" / "default.json").read_text())
    assert raw["venue"] == "paper"
    assert raw["allow_mainnet"] is False
    assert Decimal(raw["max_notional_usd"]) == Decimal("15.00")
    settings = load_settings(find_root() / "config" / "default.json")
    assert settings.venue == "paper"
    assert REQUIRED_LEVERAGE == 1
    assert REQUIRED_MARGIN == "isolated"


def test_guarded_btc_plan_passes(book, deps) -> None:
    snaps = [Snapshot.from_dict(item) for item in book]
    proposal = propose(snaps, "BTC")
    assert proposal.decision == "trade"
    assert proposal.side == "short"
    result = evaluate(proposal, _btc(book), _account(), deps.settings)
    assert result.ok, result.reasons
    assert proposal.size_usd == Decimal("10.00")


def test_notional_boundary(book, deps) -> None:
    snap = _btc(book)
    ok = _plan(size="15.00", stop="65494.20", max_loss="0.30")
    blocked = _plan(size="15.01", stop="65494.20", max_loss="0.30")
    assert evaluate(ok, snap, _account(), deps.settings).ok
    reasons = evaluate(blocked, snap, _account(), deps.settings).reasons
    assert "notional_exceeds_max" in reasons


def test_wrong_side_stop_and_wide_stop(book, deps) -> None:
    snap = _btc(book)
    wrong = _plan(size="10.00", stop="60000", max_loss="0.20")
    assert "stop_wrong_side" in evaluate(wrong, snap, _account(), deps.settings).reasons
    wide = _plan(size="15.00", stop="90000", max_loss="15.00")
    reasons = evaluate(wide, snap, _account(), deps.settings).reasons
    assert "stop_loss_exceeds_cap" in reasons


def test_daily_loss_cap(book, deps) -> None:
    snap = _btc(book)
    plan = propose([Snapshot.from_dict(item) for item in book], "BTC")
    account = {"positions": [], "realized_pnl_today": "-30.00"}
    assert "daily_loss_cap" in evaluate(plan, snap, account, deps.settings).reasons


def test_hip3_name_required(book, deps) -> None:
    bare = _plan(size="10.00", stop="65494.20", max_loss="0.20", ticker="TSLA")
    assert "ticker_not_allowlisted" in evaluate(bare, None, _account(), deps.settings).reasons
    nvda = propose([Snapshot.from_dict(item) for item in book], "xyz:NVDA")
    nvda_snap = next(Snapshot.from_dict(item) for item in book if item["ticker"] == "xyz:NVDA")
    assert evaluate(nvda, nvda_snap, _account(), deps.settings).ok


def test_env_cannot_enable_mainnet(monkeypatch) -> None:
    from hlguard.errors import ConfigError

    monkeypatch.setenv("HLGUARD_VENUE", "mainnet")
    monkeypatch.setenv("HLGUARD_ALLOW_MAINNET", "1")
    try:
        load_settings(find_root() / "config" / "default.json")
        raised = False
    except ConfigError:
        raised = True
    assert raised


def _plan(size: str, stop: str, max_loss: str, ticker: str = "BTC") -> Proposal:
    return Proposal(
        decision="trade",
        ticker=ticker,
        rationale="test",
        side="short",
        size_usd=Decimal(size),
        leverage=1,
        margin_mode="isolated",
        stop_loss=Decimal(stop),
        max_loss_usd=Decimal(max_loss),
    )
