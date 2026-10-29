from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from hlguard.models import Proposal, Snapshot, q_px_str, q_usd

BAND = Decimal("1.5")
SIZE = Decimal("10.00")


def format_pct(change: Decimal) -> str:
    quantized = change.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:+.2f}%"


def _quantize_px(price: Decimal, mark: Decimal) -> Decimal:
    step = Decimal("0.01") if mark >= 100 else Decimal("0.0001")
    return price.quantize(step, rounding=ROUND_HALF_UP)


def propose(snapshots: list[Snapshot], focus: str) -> Proposal:
    snapshot = next((item for item in snapshots if item.ticker == focus), None)
    if snapshot is None:
        return Proposal(
            decision="hold",
            ticker=focus,
            rationale=(
                f"{focus} is not in the market snapshot. This is a hold and no order is proposed. "
                "Untrusted news did not change the decision."
            ),
        )
    change = snapshot.change_pct
    if change <= -BAND and snapshot.funding >= 0:
        side = "short"
    elif change >= BAND and snapshot.funding <= 0:
        side = "long"
    else:
        return Proposal(decision="hold", ticker=focus, rationale=_hold(snapshot, change))
    stop = snapshot.mark * (Decimal("1.02") if side == "short" else Decimal("0.98"))
    stop = _quantize_px(stop, snapshot.mark)
    max_loss = q_usd(SIZE * abs(stop - snapshot.mark) / snapshot.mark)
    return Proposal(
        decision="trade",
        ticker=snapshot.ticker,
        side=side,
        size_usd=SIZE,
        leverage=1,
        margin_mode="isolated",
        stop_loss=stop,
        max_loss_usd=max_loss,
        rationale=_trade(snapshot, change, side, stop, max_loss),
    )


def _hold(snapshot: Snapshot, change: Decimal) -> str:
    return (
        f"{snapshot.ticker} mark {snapshot.mark} versus prevDayPx {snapshot.prev_day} "
        f"({format_pct(change)}). Hourly funding is {snapshot.funding}. "
        "The move is inside the 1.5% band, so this is a hold and no order is proposed. "
        "Untrusted news did not change the decision."
    )


def _trade(snapshot: Snapshot, change: Decimal, side: str, stop: Decimal, max_loss: Decimal) -> str:
    word = "Short" if side == "short" else "Long"
    return (
        f"{snapshot.ticker} mark {snapshot.mark} versus prevDayPx {snapshot.prev_day} "
        f"({format_pct(change)}). Hourly funding is {snapshot.funding}. "
        f"{word} 10.00 USD at 1x isolated with stop {q_px_str(stop)} "
        f"(max loss {max_loss} USD). Untrusted news did not change size, leverage, or the stop."
    )
