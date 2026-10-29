"""Example token prices. A missing model never fails a cycle."""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal
from pathlib import Path

from hlguard.config import find_root

_LOG = logging.getLogger("hlguard.pricing")
_QUANT = Decimal("0.0000000001")


def pricing_path() -> Path:
    override = os.environ.get("HLGUARD_PRICING", "").strip()
    if override:
        return Path(override)
    return find_root() / "config" / "pricing.json"


def load_models() -> dict:
    path = pricing_path()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        _LOG.warning("pricing table %s is unreadable; cost omitted", path)
        return {}
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, dict):
        _LOG.warning("pricing table %s has no models object; cost omitted", path)
        return {}
    return models


def quote_usd(request_model: str, response_model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    """USD for this call, or None when the model is not in the example table."""
    try:
        return _quote(request_model, response_model, input_tokens, output_tokens)
    except Exception:
        _LOG.warning("cost quote failed; holdfast.cost_usd omitted")
        return None


def _quote(request_model: str, response_model: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    models = load_models()
    priced = None
    matched = ""
    for name in (request_model, response_model):
        if name and name in models:
            priced = models[name]
            matched = name
            break
    label = request_model or response_model
    if priced is None:
        if label:
            _LOG.warning("no example price for model %s; holdfast.cost_usd omitted", label)
        return None
    try:
        in_price = Decimal(str(priced["input"]))
        out_price = Decimal(str(priced["output"]))
        incoming = Decimal(int(input_tokens))
        outgoing = Decimal(int(output_tokens))
    except (ArithmeticError, KeyError, TypeError, ValueError):
        _LOG.warning("example price for model %s is unreadable; holdfast.cost_usd omitted", matched or label)
        return None
    cost = (incoming * in_price + outgoing * out_price) / Decimal(1_000_000)
    return cost.quantize(_QUANT)
