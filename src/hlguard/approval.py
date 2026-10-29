"""Approval tokens.

A token is an HMAC over the canonical plan hash, an expiry, and a nonce.
place_order recomputes the hash from the order it actually received. A token
minted for a $10 plan does not authorize a $10,000 plan, and a nonce works once.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hlguard.errors import ApprovalError, ConfigError
from hlguard.store import update_json

GENESIS_TTL_NOTE = "v1"


class Clock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FrozenClock(Clock):
    def __init__(self, now: datetime):
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def load_secret(data_dir: Path) -> str:
    env = os.environ.get("HLGUARD_APPROVAL_SECRET", "").strip()
    if env:
        if len(env) < 16:
            raise ConfigError("HLGUARD_APPROVAL_SECRET must be at least 16 characters")
        return env
    path = data_dir / "approval_secret"
    if path.exists():
        raw = path.read_text().strip()
        if len(raw) < 16:
            raise ConfigError("approval secret file is too short")
        return raw
    raw = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw + "\n")
    os.chmod(path, 0o600)
    return raw


@dataclass
class ApprovalService:
    secret: bytes
    path: Path
    ttl_seconds: int
    clock: Clock

    @classmethod
    def create(cls, secret_text: str, data_dir: Path, ttl_seconds: int, clock: Clock) -> ApprovalService:
        return cls(
            secret=secret_text.encode(),
            path=data_dir / "approval_nonces.json",
            ttl_seconds=ttl_seconds,
            clock=clock,
        )

    def issue(self, plan_hash: str) -> str:
        if len(plan_hash) != 64:
            raise ApprovalError("bad_plan_hash")
        expires = int(self.clock.now().timestamp()) + self.ttl_seconds
        nonce = secrets.token_hex(8)
        message = f"{GENESIS_TTL_NOTE}.{plan_hash}.{expires}.{nonce}"
        signature = hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()
        return f"{message}.{signature}"

    def verify(self, token: str, plan_hash: str) -> str:
        parts = token.split(".")
        if len(parts) != 5 or parts[0] != GENESIS_TTL_NOTE:
            raise ApprovalError("malformed_token")
        _, token_hash, expires_text, nonce, signature = parts
        if not _equals(token_hash, plan_hash):
            raise ApprovalError("plan_mismatch")
        message = f"{GENESIS_TTL_NOTE}.{token_hash}.{expires_text}.{nonce}"
        expected = hmac.new(self.secret, message.encode(), hashlib.sha256).hexdigest()
        if not _equals(expected, signature):
            raise ApprovalError("bad_signature")
        try:
            expires = int(expires_text)
        except ValueError as exc:
            raise ApprovalError("malformed_token") from exc
        if expires < int(self.clock.now().timestamp()):
            raise ApprovalError("expired")
        if not nonce or len(nonce) > 64:
            raise ApprovalError("malformed_token")

        def consume(data: dict) -> dict:
            used = set(data.get("used") or [])
            if nonce in used:
                raise ApprovalError("replay")
            used.add(nonce)
            data["used"] = sorted(used)
            return data

        try:
            update_json(self.path, {"used": []}, consume)
        except ApprovalError:
            raise
        return nonce


def nonce_of(token: str) -> str:
    parts = token.split(".")
    if len(parts) != 5:
        return ""
    return parts[3]


def _equals(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    return hmac.compare_digest(left, right)
