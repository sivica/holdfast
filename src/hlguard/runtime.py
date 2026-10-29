from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from hlguard.approval import ApprovalService, Clock, load_secret
from hlguard.audit import AuditLog
from hlguard.config import Settings, default_config_path, load_settings
from hlguard.metrics import Metrics
from hlguard.paper import PaperBroker
from hlguard.store import read_json, update_json


@dataclass
class Deps:
    settings: Settings
    data_dir: Path
    config_path: Path
    clock: Clock
    audit: AuditLog
    metrics: Metrics
    broker: PaperBroker
    approvals: ApprovalService | None
    secret_text: str = field(repr=False, default="")

    def proposals_path(self) -> Path:
        return self.data_dir / "proposals.json"


def build_deps(
    config_path: Path | None = None,
    data_dir: Path | None = None,
    clock: Clock | None = None,
    *,
    with_secret: bool = True,
) -> Deps:
    path = config_path or default_config_path()
    settings = load_settings(path)
    folder = Path(data_dir or os.environ.get("HLGUARD_DATA_DIR") or Path.cwd() / "data")
    folder.mkdir(parents=True, exist_ok=True)
    used_clock = clock or Clock()
    secret = load_secret(folder) if with_secret else ""
    approvals = ApprovalService.create(secret, folder, settings.approval_ttl_seconds, used_clock) if secret else None
    return Deps(
        settings=settings,
        data_dir=folder,
        config_path=path,
        clock=used_clock,
        audit=AuditLog(folder / "audit.jsonl", used_clock),
        metrics=Metrics(folder / "metrics.json"),
        broker=PaperBroker(folder / "paper.json", used_clock),
        approvals=approvals,
        secret_text=secret,
    )


def save_proposal(deps: Deps, record: dict) -> dict:
    def add(data: dict) -> dict:
        items = data.setdefault("items", [])
        items.append(record)
        return data

    update_json(deps.proposals_path(), {"items": []}, add)
    return record


def update_proposal(deps: Deps, proposal_id: str, **fields: object) -> dict:
    found: dict = {}

    def change(data: dict) -> dict:
        for item in data.get("items") or []:
            if item.get("id") == proposal_id:
                item.update(fields)
                found.update(item)
                return data
        raise KeyError(proposal_id)

    update_json(deps.proposals_path(), {"items": []}, change)
    if not found:
        raise KeyError(proposal_id)
    return found


def get_proposal(deps: Deps, proposal_id: str) -> dict | None:
    for item in list_proposals(deps):
        if item.get("id") == proposal_id:
            return item
    return None


def list_proposals(deps: Deps) -> list[dict]:
    return list(read_json(deps.proposals_path(), {"items": []}).get("items") or [])


def child_env(scope: str, deps: Deps) -> dict[str, str]:
    """Environment for an MCP subprocess.

    The read server does not receive the approval secret or the trading key.
    A paper trade server receives the approval secret and still does not receive the key.
    """
    if scope not in {"read", "trade"}:
        raise ValueError("scope must be read or trade")
    keys = [
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "PYTHONUNBUFFERED",
        "TERM",
        "USER",
        "VIRTUAL_ENV",
    ]
    env: dict[str, str] = {}
    for key in keys:
        value = os.environ.get(key)
        if value:
            env[key] = value
    from hlguard.config import find_root

    root = find_root()
    pythonpath_parts = [part for part in os.environ.get("PYTHONPATH", "").split(":") if part]
    src_path = str(root / "src")
    if src_path not in pythonpath_parts:
        pythonpath_parts.insert(0, src_path)
    env["PYTHONPATH"] = ":".join(pythonpath_parts)
    env["HLGUARD_DATA_DIR"] = str(deps.data_dir)
    env["HLGUARD_CONFIG"] = str(deps.config_path)
    env["HLGUARD_ROOT"] = str(root)
    env["HLGUARD_VENUE"] = deps.settings.venue
    env["HLGUARD_MARKET_SOURCE"] = os.environ.get("HLGUARD_MARKET_SOURCE", "file")
    if scope == "trade":
        if not deps.secret_text:
            raise ValueError("trade scope requires an approval secret")
        env["HLGUARD_APPROVAL_SECRET"] = deps.secret_text
        if deps.settings.venue != "paper":
            for key in ("HYPERLIQUID_API_WALLET_KEY", "HLGUARD_ACCOUNT_ADDRESS", "HLGUARD_ALLOW_MAINNET"):
                value = os.environ.get(key, "")
                if value:
                    env[key] = value
    return env
