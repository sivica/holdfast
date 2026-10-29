from __future__ import annotations

import json
from decimal import Decimal

import httpx

from hlguard.config import find_root, load_settings
from hlguard.errors import ConfigError, LiveTradingError
from hlguard.live import _build_exchange, execute_live
from hlguard.market import fetch_snapshots, parse_meta_and_asset_ctxs, write_market
from hlguard.models import Snapshot
from hlguard.paper import PaperBroker
from hlguard.strategy import propose


def test_info_parser_and_dex_post(book) -> None:
    root = find_root()
    default_payload = json.loads((root / "evals" / "fixtures" / "info_default.json").read_text())
    xyz_payload = json.loads((root / "evals" / "fixtures" / "info_xyz.json").read_text())
    parsed = parse_meta_and_asset_ctxs(default_payload, "")
    assert parsed[0].ticker == "BTC"
    assert parsed[0].mark == Decimal("64000.0")
    assert parsed[0].prev_day == Decimal("65000.0")
    hip3 = parse_meta_and_asset_ctxs(xyz_payload, "xyz")
    assert hip3[0].ticker == "xyz:NVDA"
    assert hip3[0].dex == "xyz"

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content.decode()))
        body = calls[-1]
        payload = xyz_payload if body.get("dex") == "xyz" else default_payload
        return httpx.Response(200, json=payload)

    settings = load_settings(root / "config" / "default.json")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    found = fetch_snapshots(settings, client)
    assert calls[0] == {"type": "metaAndAssetCtxs"}
    assert calls[1] == {"type": "metaAndAssetCtxs", "dex": "xyz"}
    assert {item.ticker for item in found} == {"BTC", "xyz:NVDA"}


def test_stop_out_realizes_loss(deps, book) -> None:
    snaps = [Snapshot.from_dict(item) for item in book]
    write_market(deps.data_dir / "market.json", snaps)
    plan = propose(snaps, "BTC")
    snap = next(item for item in snaps if item.ticker == "BTC")
    fill = deps.broker.fill(plan, snap, deps.settings)
    assert fill["side"] == "short"
    closed = deps.broker.apply_marks({plan.ticker: Decimal(plan.stop_loss)})
    assert closed
    assert Decimal(closed[0]["pnl_usd"]) < 0
    assert deps.broker.view()["positions"] == []


def test_paper_fill_does_not_touch_the_sdk(deps, book, monkeypatch) -> None:
    monkeypatch.setenv("HYPERLIQUID_API_WALLET_KEY", "0x" + "cd" * 32)

    def boom(*_args, **_kwargs):
        raise AssertionError("sdk loaded")

    monkeypatch.setattr("hlguard.live._load_sdk", boom)
    snaps = [Snapshot.from_dict(item) for item in book]
    plan = propose(snaps, "SOL")
    snap = next(item for item in snaps if item.ticker == "SOL")
    fill = deps.broker.fill(plan, snap, deps.settings)
    assert fill["venue"] == "paper"


def test_mainnet_and_sdk_wiring(tmp_path, monkeypatch) -> None:
    root = find_root()
    base = json.loads((root / "config" / "default.json").read_text())
    path = tmp_path / "mainnet.json"
    path.write_text(json.dumps({**base, "venue": "mainnet", "allow_mainnet": True}))
    monkeypatch.delenv("HLGUARD_ALLOW_MAINNET", raising=False)
    monkeypatch.setenv("HLGUARD_VENUE", "mainnet")
    try:
        load_settings(path)
        opened = True
    except ConfigError:
        opened = False
    assert opened is False
    monkeypatch.setenv("HLGUARD_ALLOW_MAINNET", "1")
    settings = load_settings(path)
    captured: dict = {}

    class Wallet:
        address = "0xagent"

    def from_key(key: str):
        captured["key"] = key
        return Wallet()

    class Exchange:
        def __init__(self, wallet, base_url, account_address=None, perp_dexs=None):
            captured["account"] = account_address
            captured["dexs"] = perp_dexs
            captured["url"] = base_url

    monkeypatch.setattr("hlguard.live._load_sdk", lambda: (from_key, Exchange))
    monkeypatch.setenv("HYPERLIQUID_API_WALLET_KEY", "0x" + "ab" * 32)
    monkeypatch.setenv("HLGUARD_ACCOUNT_ADDRESS", "0x" + "22" * 20)
    _build_exchange(settings, "0x" + "ab" * 32, "0x" + "22" * 20)
    assert captured["dexs"] == ["", "xyz"]
    assert captured["account"] == "0x" + "22" * 20
    assert captured["url"] == settings.info_url

    class LiveExchange(Exchange):
        def update_leverage(self, leverage, name, is_cross=True):
            captured["leverage"] = (leverage, name, is_cross)
            return {"status": "ok"}

        def market_open(self, name, is_buy, sz, px, slippage):
            captured["open"] = (name, is_buy, sz, px, slippage)
            return {"status": "ok"}

        def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False):
            captured["order"] = (name, is_buy, reduce_only, order_type)
            raise RuntimeError("stop rejected")

    monkeypatch.setattr("hlguard.live._build_exchange", lambda *args, **kwargs: LiveExchange(None, ""))
    desk = json.loads((root / "evals" / "fixtures" / "desk.json").read_text())
    snaps = [Snapshot.from_dict(item) for item in desk["snapshots"]]
    plan = propose(snaps, "BTC")
    snap = next(item for item in snaps if item.ticker == "BTC")
    try:
        execute_live(settings, plan, snap)
        raised = False
    except LiveTradingError as exc:
        raised = True
        assert "0x" + "ab" * 32 not in str(exc)
    assert raised
    assert captured["leverage"] == (1, "BTC", False)
    assert captured["open"][0] == "BTC"
    assert captured["open"][1] is False
    broker = PaperBroker(tmp_path / "paper.json", deps_clock())
    assert broker.view()["positions"] == []


def deps_clock():
    from hlguard.approval import Clock

    return Clock()
