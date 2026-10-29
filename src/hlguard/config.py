from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from hlguard.errors import ConfigError


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    venue: str
    allow_mainnet: bool
    max_notional_usd: Decimal
    daily_loss_cap_usd: Decimal
    max_loss_per_trade_usd: Decimal
    slippage_bps: Decimal
    approval_ttl_seconds: int
    allowlist: list[str]
    info_url: str
    testnet_info_url: str

    @field_validator("venue")
    @classmethod
    def _venue(cls, value: str) -> str:
        if value not in {"paper", "testnet", "mainnet"}:
            raise ValueError("venue must be paper, testnet, or mainnet")
        return value

    @field_validator("allowlist")
    @classmethod
    def _allowlist(cls, value: list[str]) -> list[str]:
        if not value or any((not name) or (" " in name) for name in value):
            raise ValueError("allowlist entries must be non-empty and contain no spaces")
        return value

    @field_validator("max_notional_usd", "daily_loss_cap_usd", "max_loss_per_trade_usd", "slippage_bps")
    @classmethod
    def _positive(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("limits must be positive")
        return value


def find_root() -> Path:
    env = os.environ.get("HLGUARD_ROOT", "").strip()
    if env:
        return Path(env)
    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "config" / "default.json").exists() and (candidate / "evals" / "scenarios.json").exists():
            return candidate
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "config" / "default.json").exists():
            return candidate
    return cwd


def default_config_path() -> Path:
    override = os.environ.get("HLGUARD_CONFIG", "").strip()
    if override:
        return Path(override)
    return find_root() / "config" / "default.json"


def load_settings(path: Path | None = None) -> Settings:
    config_path = path or default_config_path()
    try:
        data = json.loads(config_path.read_text())
    except FileNotFoundError as exc:
        raise ConfigError(f"config not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError("config file is not valid JSON") from exc
    # Venue may be pinned by the operator. allow_mainnet cannot be flipped from the environment.
    venue = os.environ.get("HLGUARD_VENUE", "").strip()
    if venue:
        data["venue"] = venue
    try:
        settings = Settings.model_validate(data)
    except Exception as exc:
        raise ConfigError(str(exc)) from None
    if settings.venue == "mainnet":
        if not settings.allow_mainnet:
            raise ConfigError("mainnet refused: set allow_mainnet to true in the config file")
        if os.environ.get("HLGUARD_ALLOW_MAINNET") != "1":
            raise ConfigError("mainnet refused: set HLGUARD_ALLOW_MAINNET=1")
    return settings


def info_url_for(settings: Settings) -> str:
    if settings.venue == "testnet":
        return settings.testnet_info_url
    return settings.info_url
