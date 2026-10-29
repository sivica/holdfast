from __future__ import annotations

from hlguard.approval import ApprovalError
from hlguard.config import Settings
from hlguard.errors import ConfigError, LiveTradingError, MarketError
from hlguard.live import assert_live_allowed, execute_live
from hlguard.market import load_snapshots
from hlguard.models import Proposal, Snapshot, proposal_from_canonical, q_usd
from hlguard.news import load_news, suspicion
from hlguard.policy import evaluate, risk_used
from hlguard.runtime import Deps


def list_markets(deps: Deps) -> dict:
    deps.metrics.inc("tool.list_markets")
    try:
        snapshots = _allowlisted(deps)
    except MarketError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "markets": [snapshot.to_dict() for snapshot in snapshots]}


def get_market(deps: Deps, ticker: str) -> dict:
    deps.metrics.inc("tool.get_market")
    if ticker not in deps.settings.allowlist:
        return {"ok": False, "error": "ticker_not_allowlisted"}
    try:
        snapshots = _allowlisted(deps)
    except MarketError as exc:
        return {"ok": False, "error": str(exc)}
    found = next((snapshot for snapshot in snapshots if snapshot.ticker == ticker), None)
    if found is None:
        return {"ok": False, "error": "market_unavailable"}
    return {"ok": True, "market": found.to_dict()}


def get_news(deps: Deps) -> dict:
    deps.metrics.inc("tool.get_news")
    items = load_news(deps.data_dir / "news.json")
    labels = suspicion(items)
    return {
        "ok": True,
        "trust": "untrusted",
        "injection_suspected": bool(labels),
        "injection_labels": labels,
        "items": [item.to_dict() for item in items],
    }


def get_account(deps: Deps) -> dict:
    deps.metrics.inc("tool.get_account")
    state = deps.broker.view()
    return {
        "ok": True,
        "source": "local-ledger",
        "venue": deps.settings.venue,
        "equity_usd": state["equity_usd"],
        "realized_pnl_today": state["realized_pnl_today"],
        "daily_loss_used_usd": format(q_usd(risk_used(state)), "f"),
        "daily_loss_cap_usd": format(deps.settings.daily_loss_cap_usd, "f"),
        "max_notional_usd": format(deps.settings.max_notional_usd, "f"),
        "positions": state["positions"],
    }


def place_order(deps: Deps, arguments: dict) -> dict:
    deps.metrics.inc("tool.place_order")
    if deps.approvals is None:
        return _deny(deps, "no_approval_service", ["no_approval_service"])
    try:
        proposal = Proposal(
            decision="trade",
            ticker=str(arguments.get("ticker") or ""),
            rationale="",
            side=None if arguments.get("side") is None else str(arguments.get("side")),
            size_usd=_maybe(arguments.get("size_usd")),
            leverage=_maybe_lev(arguments.get("leverage")),
            margin_mode=None if arguments.get("margin_mode") is None else str(arguments.get("margin_mode")),
            stop_loss=_maybe(arguments.get("stop_loss")),
            max_loss_usd=_maybe(arguments.get("max_loss_usd")),
        )
        canonical = proposal.canonical()
    except Exception:
        return _deny(deps, "malformed_order", ["malformed_order"])
    plan_hash = proposal.plan_hash()
    token = str(arguments.get("approval_token") or "")
    try:
        deps.approvals.verify(token, plan_hash)
    except ApprovalError as exc:
        return _deny(deps, exc.code, [exc.code], plan_hash=plan_hash)
    snapshot = _snapshot_for(deps, canonical["ticker"])
    account = deps.broker.view()
    checked = proposal_from_canonical(canonical, "")
    result = evaluate(checked, snapshot, account, deps.settings)
    if not result.ok:
        return _deny(deps, "policy", result.reasons, plan_hash=plan_hash)
    if snapshot is None:
        return _deny(deps, "market_unavailable", ["market_unavailable"], plan_hash=plan_hash)
    try:
        if deps.settings.venue == "paper":
            fill = deps.broker.fill(checked, snapshot, deps.settings)
        else:
            assert_live_allowed(deps.settings)
            fill = execute_live(deps.settings, checked, snapshot)
            deps.broker.fill(checked, snapshot, deps.settings)
    except (ConfigError, LiveTradingError, MarketError) as exc:
        return _deny(deps, str(exc), [str(exc)], plan_hash=plan_hash)
    deps.metrics.inc("fills")
    deps.audit.append(
        {
            "event": "order",
            "fill": fill,
            "plan_hash": plan_hash,
            "ticker": canonical["ticker"],
            "venue": deps.settings.venue,
        }
    )
    return {"ok": True, "fill": fill, "plan_hash": plan_hash}


def _deny(deps: Deps, error: str, reasons: list[str], plan_hash: str | None = None) -> dict:
    deps.metrics.inc("policy_denials")
    deps.audit.append(
        {
            "event": "order_denied",
            "error": error,
            "plan_hash": plan_hash,
            "reasons": reasons,
        }
    )
    return {"ok": False, "error": error, "reasons": reasons}


def _allowlisted(deps: Deps) -> list[Snapshot]:
    allow = set(deps.settings.allowlist)
    return [snapshot for snapshot in load_snapshots(deps.settings, deps.data_dir) if snapshot.ticker in allow]


def _snapshot_for(deps: Deps, ticker: str) -> Snapshot | None:
    if ticker not in deps.settings.allowlist:
        return None
    try:
        snapshots = _allowlisted(deps)
    except MarketError:
        return None
    return next((snapshot for snapshot in snapshots if snapshot.ticker == ticker), None)


def _maybe(value: object):
    from decimal import Decimal, InvalidOperation

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", ""))
    except InvalidOperation:
        return None


def _maybe_lev(value: object) -> int | None:
    from hlguard.errors import InvalidOrder
    from hlguard.models import parse_leverage

    if value is None:
        return None
    try:
        return parse_leverage(value)
    except InvalidOrder:
        return None


def account_for_policy(settings: Settings, state: dict) -> dict:
    return state
