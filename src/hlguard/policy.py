"""Hard risk limits.

The model can ask for anything. place_order calls evaluate again after the
human approval token is checked, and these constants are not configurable
from the prompt, the news feed, or an environment variable.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from hlguard.config import Settings
from hlguard.models import REQUIRED_LEVERAGE, REQUIRED_MARGIN, Proposal, Snapshot, q_usd

MIN_STOP_DISTANCE = Decimal("0.002")


@dataclass(frozen=True)
class PolicyResult:
    ok: bool
    reasons: list[str]


def risk_used(account: dict) -> Decimal:
    realized = Decimal(str(account.get("realized_pnl_today") or "0"))
    open_risk = sum(
        (Decimal(str(position["max_loss_usd"])) for position in account.get("positions") or []),
        Decimal("0"),
    )
    return -min(realized, Decimal("0")) + open_risk


def evaluate(
    proposal: Proposal,
    snapshot: Snapshot | None,
    account: dict,
    settings: Settings,
) -> PolicyResult:
    if proposal.decision == "hold":
        return PolicyResult(ok=True, reasons=[])
    if proposal.decision != "trade":
        return PolicyResult(ok=False, reasons=["bad_decision"])

    reasons: list[str] = []
    if proposal.ticker not in settings.allowlist:
        reasons.append("ticker_not_allowlisted")
    if proposal.side not in {"long", "short"}:
        reasons.append("side_invalid")
    if proposal.margin_mode != REQUIRED_MARGIN:
        reasons.append("margin_must_be_isolated")
    if proposal.leverage != REQUIRED_LEVERAGE:
        reasons.append("leverage_must_be_1")

    size = None if proposal.size_usd is None else q_usd(proposal.size_usd)
    if size is None:
        reasons.append("size_required")
    elif size <= 0:
        reasons.append("notional_not_positive")
    elif size > settings.max_notional_usd:
        reasons.append("notional_exceeds_max")

    if proposal.stop_loss is None:
        reasons.append("stop_loss_required")
    if proposal.max_loss_usd is None:
        reasons.append("max_loss_required")
    elif q_usd(proposal.max_loss_usd) <= 0:
        reasons.append("max_loss_not_positive")
    elif q_usd(proposal.max_loss_usd) > settings.max_loss_per_trade_usd:
        reasons.append("max_loss_exceeds_cap")

    ticker_ok = proposal.ticker in settings.allowlist
    if ticker_ok and snapshot is None:
        reasons.append("market_unavailable")
    elif (
        ticker_ok
        and snapshot is not None
        and proposal.stop_loss is not None
        and proposal.side in {"long", "short"}
        and size is not None
        and size > 0
    ):
        _check_stop(reasons, proposal, snapshot, size, settings)

    if proposal.max_loss_usd is not None and q_usd(proposal.max_loss_usd) > 0:
        if risk_used(account) + q_usd(proposal.max_loss_usd) > settings.daily_loss_cap_usd:
            reasons.append("daily_loss_cap")

    unique = sorted(set(reasons))
    return PolicyResult(ok=not unique, reasons=unique)


def _check_stop(
    reasons: list[str],
    proposal: Proposal,
    snapshot: Snapshot,
    size: Decimal,
    settings: Settings,
) -> None:
    assert proposal.stop_loss is not None
    mark = snapshot.mark
    stop = proposal.stop_loss
    if mark <= 0:
        reasons.append("market_unavailable")
        return
    if proposal.side == "long" and stop >= mark:
        reasons.append("stop_wrong_side")
    if proposal.side == "short" and stop <= mark:
        reasons.append("stop_wrong_side")
    distance = abs(stop - mark) / mark
    computed = q_usd(size * abs(stop - mark) / mark)
    if distance < MIN_STOP_DISTANCE or computed <= 0:
        reasons.append("stop_not_protective")
    if computed > settings.max_loss_per_trade_usd:
        reasons.append("stop_loss_exceeds_cap")
    if proposal.max_loss_usd is not None and q_usd(proposal.max_loss_usd) + Decimal("0") < computed * Decimal("0.95"):
        reasons.append("max_loss_understated")
