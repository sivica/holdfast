from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from hlguard.errors import InvalidOrder, ParseError

REQUIRED_LEVERAGE = 1
REQUIRED_MARGIN = "isolated"


def q_usd(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def q_px_str(value: Decimal) -> str:
    quantized = Decimal(value).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def as_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool) or value is None:
        raise InvalidOrder("bad_number")
    if isinstance(value, float):
        return Decimal(format(value, "f"))
    text = str(value).strip().replace(",", "").replace("$", "").replace("_", "")
    if not text:
        raise InvalidOrder("bad_number")
    return Decimal(text)


def parse_leverage(value: object) -> int:
    if isinstance(value, bool) or value is None:
        raise InvalidOrder("bad_leverage")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        sign = 1
        if text.startswith("-"):
            sign = -1
            text = text[1:]
        if text.isdigit():
            return sign * int(text)
    raise InvalidOrder("bad_leverage")


def hash_canonical(canonical: dict) -> str:
    raw = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def canonicalize_order(
    *,
    ticker: object,
    side: object,
    size_usd: object,
    leverage: object,
    stop_loss: object,
    max_loss_usd: object,
    margin_mode: object,
) -> dict:
    if ticker is None or side is None or size_usd is None or stop_loss is None or max_loss_usd is None:
        raise InvalidOrder("incomplete_order")
    if margin_mode is None:
        raise InvalidOrder("incomplete_order")
    return {
        "leverage": parse_leverage(leverage),
        "margin_mode": str(margin_mode),
        "max_loss_usd": format(q_usd(as_decimal(max_loss_usd)), "f"),
        "side": str(side),
        "size_usd": format(q_usd(as_decimal(size_usd)), "f"),
        "stop_loss": q_px_str(as_decimal(stop_loss)),
        "ticker": str(ticker),
    }


@dataclass
class Snapshot:
    ticker: str
    mark: Decimal
    prev_day: Decimal
    funding: Decimal
    sz_decimals: int
    dex: str

    @classmethod
    def from_dict(cls, raw: dict) -> Snapshot:
        return cls(
            ticker=str(raw["ticker"]),
            mark=Decimal(str(raw["mark"])),
            prev_day=Decimal(str(raw["prev_day"])),
            funding=Decimal(str(raw["funding"])),
            sz_decimals=int(raw["sz_decimals"]),
            dex=str(raw.get("dex") or ""),
        )

    @property
    def change_pct(self) -> Decimal:
        if self.prev_day == 0:
            return Decimal("0")
        return (self.mark - self.prev_day) / self.prev_day * Decimal(100)

    def to_dict(self) -> dict:
        change = self.change_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return {
            "ticker": self.ticker,
            "mark": format(self.mark, "f"),
            "prev_day": format(self.prev_day, "f"),
            "funding": format(self.funding, "f"),
            "sz_decimals": self.sz_decimals,
            "dex": self.dex,
            "change_pct": format(change, "f"),
        }


@dataclass
class NewsItem:
    source: str
    title: str
    text: str

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "title": self.title,
            "text": self.text,
            "trust": "untrusted",
        }


@dataclass
class Proposal:
    decision: str
    ticker: str
    rationale: str
    side: str | None = None
    size_usd: Decimal | None = None
    leverage: int | None = None
    margin_mode: str | None = None
    stop_loss: Decimal | None = None
    max_loss_usd: Decimal | None = None

    def canonical(self) -> dict:
        return canonicalize_order(
            ticker=self.ticker,
            side=self.side,
            size_usd=self.size_usd,
            leverage=self.leverage,
            stop_loss=self.stop_loss,
            max_loss_usd=self.max_loss_usd,
            margin_mode=self.margin_mode,
        )

    def plan_hash(self) -> str:
        return hash_canonical(self.canonical())

    def order_args(self, approval_token: str) -> dict[str, object]:
        canonical = self.canonical()
        return {
            "ticker": canonical["ticker"],
            "side": canonical["side"],
            "size_usd": canonical["size_usd"],
            "leverage": canonical["leverage"],
            "stop_loss": canonical["stop_loss"],
            "max_loss_usd": canonical["max_loss_usd"],
            "margin_mode": canonical["margin_mode"],
            "approval_token": approval_token,
        }

    def display(self) -> dict:
        def num(value: Decimal | None) -> str | None:
            if value is None:
                return None
            return format(value, "f")

        return {
            "ticker": self.ticker,
            "side": self.side,
            "size_usd": num(self.size_usd),
            "leverage": self.leverage,
            "margin_mode": self.margin_mode,
            "stop_loss": None if self.stop_loss is None else q_px_str(self.stop_loss),
            "max_loss_usd": None if self.max_loss_usd is None else format(q_usd(self.max_loss_usd), "f"),
        }


def iter_json_dicts(text: str) -> list[dict]:
    decoder = json.JSONDecoder()
    found: list[dict] = []
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(value, dict):
            found.append(value)
        index = end
    return found


def _maybe_decimal(value: object) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return as_decimal(value)
    except (InvalidOrder, ValueError, ArithmeticError):
        return None


def proposal_from_model(obj: dict) -> Proposal:
    decision = str(obj.get("decision", "")).strip().lower()
    if decision not in {"trade", "hold"}:
        raise ParseError("bad_decision")
    ticker = str(obj.get("ticker") or "")
    rationale = str(obj.get("rationale") or "")[:2000]
    if decision == "hold":
        return Proposal(decision="hold", ticker=ticker, rationale=rationale)
    leverage: int | None
    try:
        leverage = None if obj.get("leverage") is None else parse_leverage(obj.get("leverage"))
    except InvalidOrder:
        leverage = None
    margin = obj.get("margin_mode")
    side = obj.get("side")
    return Proposal(
        decision="trade",
        ticker=ticker,
        rationale=rationale,
        side=None if side is None else str(side),
        size_usd=_maybe_decimal(obj.get("size_usd")),
        leverage=leverage,
        margin_mode=None if margin is None else str(margin),
        stop_loss=_maybe_decimal(obj.get("stop_loss")),
        max_loss_usd=_maybe_decimal(obj.get("max_loss_usd")),
    )


def parse_proposal(text: str) -> Proposal:
    # The last object wins. News echoed at the front of a completion cannot
    # override a plan the model appended, and a completion that is only the
    # injected object is still parsed and then rejected by policy.
    candidates = [obj for obj in iter_json_dicts(text) if "decision" in obj]
    if not candidates:
        raise ParseError("unparseable_model_output")
    return proposal_from_model(candidates[-1])


def proposal_from_canonical(canonical: dict, rationale: str) -> Proposal:
    return Proposal(
        decision="trade",
        ticker=str(canonical["ticker"]),
        rationale=rationale,
        side=str(canonical["side"]),
        size_usd=Decimal(str(canonical["size_usd"])),
        leverage=int(canonical["leverage"]),
        margin_mode=str(canonical["margin_mode"]),
        stop_loss=Decimal(str(canonical["stop_loss"])),
        max_loss_usd=Decimal(str(canonical["max_loss_usd"])),
    )
