"""Optional exchange path.

Paper mode never imports the SDK and never reads the wallet key. Testnet and
mainnet go through the official hyperliquid-python-sdk the way an agent wallet
is used: the key comes from HYPERLIQUID_API_WALLET_KEY, and the main account
is HLGUARD_ACCOUNT_ADDRESS. HIP-3 names such as xyz:NVDA need perp_dexs to
include "xyz" so the SDK can resolve the asset index.

The SDK's meta_and_asset_ctxs() does not take a dex argument (upstream issue
278). Market data therefore uses the public /info endpoint directly.
"""

from __future__ import annotations

import os
from decimal import ROUND_DOWN, Decimal

from hlguard.config import Settings, info_url_for
from hlguard.errors import ConfigError, LiveTradingError
from hlguard.models import Proposal, Snapshot, q_usd
from hlguard.redact import redact


def assert_live_allowed(settings: Settings) -> None:
    if settings.venue == "paper":
        raise ConfigError("paper venue cannot call the exchange")
    if settings.venue == "testnet":
        return
    if settings.venue != "mainnet":
        raise ConfigError("unknown venue")
    if not settings.allow_mainnet or os.environ.get("HLGUARD_ALLOW_MAINNET") != "1":
        raise ConfigError("mainnet refused")


def _load_sdk():
    import eth_account
    from hyperliquid.exchange import Exchange

    return eth_account.Account.from_key, Exchange


def _build_exchange(settings: Settings, key: str, account: str):
    from_key, exchange_cls = _load_sdk()
    wallet = from_key(key)
    return exchange_cls(
        wallet,
        info_url_for(settings),
        account_address=account,
        perp_dexs=["", "xyz"],
    )


def execute_live(settings: Settings, proposal: Proposal, snapshot: Snapshot) -> dict:
    assert_live_allowed(settings)
    key = os.environ.get("HYPERLIQUID_API_WALLET_KEY", "").strip()
    account = os.environ.get("HLGUARD_ACCOUNT_ADDRESS", "").strip()
    if not key or not account:
        raise LiveTradingError("missing HYPERLIQUID_API_WALLET_KEY or HLGUARD_ACCOUNT_ADDRESS")
    if proposal.stop_loss is None or proposal.size_usd is None or proposal.side not in {"long", "short"}:
        raise LiveTradingError("incomplete_order")
    try:
        exchange = _build_exchange(settings, key, account)
        is_buy = proposal.side == "long"
        price = snapshot.mark
        quantum = Decimal("1").scaleb(-snapshot.sz_decimals)
        size = (q_usd(proposal.size_usd) / price).quantize(quantum, rounding=ROUND_DOWN)
        if size <= 0:
            raise LiveTradingError("size_rounds_to_zero")
        size_float = float(size)
        coin = proposal.ticker
        exchange.update_leverage(1, coin, is_cross=False)
        opened = exchange.market_open(coin, is_buy, size_float, None, 0.01)
        stop_is_buy = not is_buy
        stop = proposal.stop_loss
        limit = stop * (Decimal("1.01") if stop_is_buy else Decimal("0.99"))
        try:
            stopped = exchange.order(
                coin,
                stop_is_buy,
                size_float,
                float(limit),
                {"trigger": {"triggerPx": float(stop), "isMarket": True, "tpsl": "sl"}},
                reduce_only=True,
            )
        except Exception as exc:
            raise LiveTradingError("entry filled but stop failed: " + redact(str(exc))) from None
    except LiveTradingError:
        raise
    except Exception as exc:
        raise LiveTradingError(redact(str(exc))) from None
    return {
        "entry": _public_response(opened),
        "leverage": 1,
        "margin_mode": "isolated",
        "notional_usd": format(q_usd(proposal.size_usd), "f"),
        "side": proposal.side,
        "size_coin": format(size, "f"),
        "stop": _public_response(stopped),
        "stop_loss": format(proposal.stop_loss, "f"),
        "ticker": proposal.ticker,
        "venue": settings.venue,
    }


def _public_response(payload: object) -> object:
    if isinstance(payload, dict):
        return {key: _public_response(value) for key, value in payload.items() if key != "signature"}
    if isinstance(payload, list):
        return [_public_response(item) for item in payload]
    if isinstance(payload, str):
        return redact(payload)
    return payload
