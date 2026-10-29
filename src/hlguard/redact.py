from __future__ import annotations

import os
import re

_HEX_KEY = re.compile(r"0x[a-fA-F0-9]{64}")
_ASSIGNMENT = re.compile(
    r"(?i)\b(HYPERLIQUID_API_WALLET_KEY|HLGUARD_APPROVAL_SECRET|HLGUARD_ACCOUNT_ADDRESS|"
    r"OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*[=:]\s*\S+"
)


def secret_values() -> list[str]:
    values: list[str] = []
    for name in (
        "HYPERLIQUID_API_WALLET_KEY",
        "HLGUARD_APPROVAL_SECRET",
        "HLGUARD_ACCOUNT_ADDRESS",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ):
        raw = os.environ.get(name, "").strip()
        if len(raw) >= 8:
            values.append(raw)
    return values


def redact(text: str, secrets: list[str] | None = None) -> str:
    out = "" if text is None else str(text)
    for secret in secrets if secrets is not None else secret_values():
        if secret and len(secret) >= 8 and secret in out:
            out = out.replace(secret, "[REDACTED]")
    out = _HEX_KEY.sub("0x[REDACTED]", out)
    return _ASSIGNMENT.sub(lambda match: match.group(1) + "=[REDACTED]", out)
