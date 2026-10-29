from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

from hlguard.approval import Clock
from hlguard.config import Settings
from hlguard.errors import MarketError
from hlguard.models import Proposal, Snapshot, q_px_str, q_usd
from hlguard.store import read_json, update_json


def _empty(day: str) -> dict:
    return {
        "day": day,
        "equity_usd": "1000.00",
        "orders": [],
        "positions": [],
        "realized_pnl_today": "0.00",
    }


class PaperBroker:
    def __init__(self, path: Path, clock: Clock):
        self.path = path
        self.clock = clock

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def _normalize(self, state: dict) -> dict:
        today = self._today()
        if state.get("day") != today:
            state["day"] = today
            state["realized_pnl_today"] = "0.00"
        state.setdefault("equity_usd", "1000.00")
        state.setdefault("orders", [])
        state.setdefault("positions", [])
        return state

    def view(self) -> dict:
        state = self._normalize(read_json(self.path, _empty(self._today())))
        if not self.path.exists() or read_json(self.path, {}).get("day") != state["day"]:
            update_json(self.path, _empty(self._today()), lambda _data: state)
        return state

    def fill(self, proposal: Proposal, snapshot: Snapshot, settings: Settings) -> dict:
        if proposal.stop_loss is None or proposal.size_usd is None or proposal.max_loss_usd is None:
            raise MarketError("incomplete_order")
        slip = settings.slippage_bps / Decimal("10000")
        if proposal.side == "long":
            price = snapshot.mark * (Decimal("1") + slip)
        else:
            price = snapshot.mark * (Decimal("1") - slip)
        price = price.quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        quantum = Decimal("1").scaleb(-snapshot.sz_decimals)
        size = (q_usd(proposal.size_usd) / price).quantize(quantum, rounding=ROUND_DOWN)
        if size <= 0:
            raise MarketError("size_rounds_to_zero")
        notional = (size * price).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
        if notional <= 0 or notional > settings.max_notional_usd:
            raise MarketError("fill_notional_rejected")
        opened = self.clock.now().isoformat()
        position = {
            "entry_px": format(price, "f"),
            "id": str(uuid.uuid4()),
            "leverage": 1,
            "margin_mode": "isolated",
            "max_loss_usd": format(q_usd(proposal.max_loss_usd), "f"),
            "opened_at": opened,
            "side": proposal.side,
            "size_coin": format(size, "f"),
            "stop_loss": q_px_str(proposal.stop_loss),
            "ticker": proposal.ticker,
        }
        order_id = str(uuid.uuid4())
        stop_order_id = str(uuid.uuid4())

        def add(state: dict) -> dict:
            state = self._normalize(state)
            state["positions"].append(position)
            state["orders"].append(
                {
                    "id": order_id,
                    "kind": "entry",
                    "notional_usd": format(notional, "f"),
                    "price": format(price, "f"),
                    "side": proposal.side,
                    "ticker": proposal.ticker,
                    "ts": opened,
                }
            )
            state["orders"].append(
                {
                    "id": stop_order_id,
                    "kind": "stop",
                    "price": q_px_str(proposal.stop_loss),
                    "side": "long" if proposal.side == "short" else "short",
                    "ticker": proposal.ticker,
                    "ts": opened,
                }
            )
            return state

        update_json(self.path, _empty(self._today()), add)
        return {
            "fill_px": format(price, "f"),
            "leverage": 1,
            "margin_mode": "isolated",
            "notional_usd": format(notional, "f"),
            "order_id": order_id,
            "side": proposal.side,
            "size_coin": format(size, "f"),
            "stop_loss": q_px_str(proposal.stop_loss),
            "stop_order_id": stop_order_id,
            "ticker": proposal.ticker,
            "venue": "paper",
        }

    def apply_marks(self, prices: dict[str, Decimal]) -> list[dict]:
        closed: list[dict] = []

        def mark(state: dict) -> dict:
            state = self._normalize(state)
            still = []
            for position in state["positions"]:
                price = prices.get(position["ticker"])
                if price is None or not _stopped(position, price):
                    still.append(position)
                    continue
                exit_px = _exit_price(position, price)
                pnl = _pnl(position, exit_px)
                state["realized_pnl_today"] = format(
                    q_usd(Decimal(state["realized_pnl_today"]) + pnl),
                    "f",
                )
                state["equity_usd"] = format(q_usd(Decimal(state["equity_usd"]) + pnl), "f")
                closed.append(
                    {
                        "exit_px": format(exit_px, "f"),
                        "pnl_usd": format(q_usd(pnl), "f"),
                        "ticker": position["ticker"],
                    }
                )
            state["positions"] = still
            return state

        update_json(self.path, _empty(self._today()), mark)
        return closed


def _stopped(position: dict, mark: Decimal) -> bool:
    stop = Decimal(position["stop_loss"])
    if position["side"] == "long":
        return mark <= stop
    return mark >= stop


def _exit_price(position: dict, mark: Decimal) -> Decimal:
    stop = Decimal(position["stop_loss"])
    if position["side"] == "long":
        return min(stop, mark)
    return max(stop, mark)


def _pnl(position: dict, exit_px: Decimal) -> Decimal:
    entry = Decimal(position["entry_px"])
    size = Decimal(position["size_coin"])
    if position["side"] == "long":
        return (exit_px - entry) * size
    return (entry - exit_px) * size
