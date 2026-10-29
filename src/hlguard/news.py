from __future__ import annotations

import json
import re
from pathlib import Path

from hlguard.models import NewsItem

# Observability only. A match does not block, and a miss does not allow.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_limits", re.compile(r"ignore\s+(all\s+|the\s+)?(previous|prior|limits|policy)", re.I)),
    ("role_override", re.compile(r"you\s+are\s+now|system\s*:", re.I)),
    ("skip_stop", re.compile(r"no\s+stop|stop\s+is\s+optional|without\s+a\s+stop", re.I)),
    ("oversized", re.compile(r"\$\s*10[,.]?000|\b10k\b", re.I)),
    ("allowlist_bypass", re.compile(r"disregard\s+the\s+allowlist", re.I)),
    ("cross_margin", re.compile(r"\bcross\b", re.I)),
]


def detect(text: str) -> list[str]:
    return [name for name, pattern in _PATTERNS if pattern.search(text)]


def load_news(path: Path) -> list[NewsItem]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text())
    return [
        NewsItem(
            source=str(item.get("source") or "unknown"),
            title=str(item.get("title") or ""),
            text=str(item.get("text") or ""),
        )
        for item in payload.get("items") or []
    ]


def write_news(path: Path, items: list[NewsItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"items": [{"source": item.source, "title": item.title, "text": item.text} for item in items]}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))


def suspicion(items: list[NewsItem]) -> list[str]:
    labels: set[str] = set()
    for item in items:
        labels.update(detect(f"{item.title}\n{item.text}"))
    return sorted(labels)
