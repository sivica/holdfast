from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

import httpx

from hlguard.config import Settings, info_url_for
from hlguard.errors import MarketError
from hlguard.models import Snapshot


def write_market(path: Path, snapshots: list[Snapshot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"snapshots": [snapshot.to_dict() for snapshot in snapshots]}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def read_market(path: Path) -> list[Snapshot]:
    if not path.exists():
        raise MarketError("market file is missing")
    payload = json.loads(path.read_text())
    return [Snapshot.from_dict(item) for item in payload.get("snapshots") or []]


def parse_meta_and_asset_ctxs(payload: object, dex: str) -> list[Snapshot]:
    if not isinstance(payload, list) or len(payload) != 2:
        raise MarketError("unexpected metaAndAssetCtxs payload")
    meta, contexts = payload
    if not isinstance(meta, dict) or not isinstance(contexts, list):
        raise MarketError("unexpected metaAndAssetCtxs payload")
    universe = meta.get("universe") or []
    snapshots: list[Snapshot] = []
    for asset, context in zip(universe, contexts):
        if not isinstance(asset, dict) or not isinstance(context, dict):
            continue
        mark = context.get("markPx")
        prev = context.get("prevDayPx")
        if mark is None or prev is None or not asset.get("name"):
            continue
        snapshots.append(
            Snapshot(
                ticker=str(asset["name"]),
                mark=Decimal(str(mark)),
                prev_day=Decimal(str(prev)),
                funding=Decimal(str(context.get("funding") or "0")),
                sz_decimals=int(asset.get("szDecimals") or 0),
                dex=dex,
            )
        )
    return snapshots


def fetch_snapshots(settings: Settings, client: httpx.Client | None = None) -> list[Snapshot]:
    owns_client = client is None
    http = client or httpx.Client(timeout=httpx.Timeout(8.0), headers={"User-Agent": "holdfast/0.1"})
    try:
        url = info_url_for(settings).rstrip("/") + "/info"
        found: list[Snapshot] = []
        for dex in ("", "xyz"):
            body: dict[str, str] = {"type": "metaAndAssetCtxs"}
            if dex:
                body["dex"] = dex
            try:
                response = http.post(url, json=body)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise MarketError(f"info API request failed for dex {dex or 'default'}") from exc
            found.extend(parse_meta_and_asset_ctxs(response.json(), dex))
        allow = set(settings.allowlist)
        return [snapshot for snapshot in found if snapshot.ticker in allow]
    finally:
        if owns_client:
            http.close()


def load_snapshots(settings: Settings, data_dir: Path) -> list[Snapshot]:
    source = os.environ.get("HLGUARD_MARKET_SOURCE", "file").strip() or "file"
    if source == "live":
        return fetch_snapshots(settings)
    if source != "file":
        raise MarketError("HLGUARD_MARKET_SOURCE must be file or live")
    return read_market(data_dir / "market.json")
