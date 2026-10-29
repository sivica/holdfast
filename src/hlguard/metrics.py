"""Process metrics stored in one JSON file.

Counters are integers. `llm_cost_usd` is a float running total. Histogram state
lives under `_histograms` and is omitted from `snapshot()`. Non-numeric values
are not exported: they are skipped, and a debug log names the key.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from hlguard.store import read_json, update_json

_LOG = logging.getLogger("hlguard.metrics")

# Upper bounds for holdfast_llm_call_latency_seconds, in seconds.
# The +Inf bucket is the observation count and is not stored here.
LLM_LATENCY_BUCKETS: tuple[tuple[float, str], ...] = (
    (0.005, "0.005"),
    (0.01, "0.01"),
    (0.025, "0.025"),
    (0.05, "0.05"),
    (0.1, "0.1"),
    (0.25, "0.25"),
    (0.5, "0.5"),
    (1.0, "1"),
    (2.5, "2.5"),
    (5.0, "5"),
    (10.0, "10"),
    (30.0, "30"),
    (60.0, "60"),
    (120.0, "120"),
)

# Counters the process already increments. Always exported so a scrape before
# the first event still has a stable series. Extra integer keys are exported too.
_COUNTER_HELP: tuple[tuple[str, str], ...] = (
    ("proposals", "Orders proposed by the analyst."),
    ("approvals", "Orders approved by a human."),
    ("rejections", "Orders rejected by a human."),
    ("orders", "Orders filled after approval."),
    ("fills", "Fills recorded by place_order."),
    ("order_failures", "Approved orders that failed to fill."),
    ("policy_denials", "Policy denials."),
    ("blocked", "Proposals blocked before approval."),
    ("holds", "Hold decisions."),
)

_FLOAT_HELP: dict[str, str] = {
    "llm_cost_usd": "Running total of example LLM cost in USD. Not a bill.",
}

_TOOLS: tuple[str, ...] = (
    "get_account",
    "get_market",
    "get_news",
    "list_markets",
    "place_order",
)

_HISTOGRAMS: tuple[tuple[str, str], ...] = (("llm_call_latency_seconds", "Latency of LLM complete calls, in seconds."),)

_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


class Metrics:
    def __init__(self, path: Path):
        self.path = path

    def inc(self, name: str, amount: int = 1) -> None:
        def add(data: dict) -> dict:
            data[name] = int(data.get(name, 0)) + amount
            return data

        update_json(self.path, {}, add)

    def add_decimal(self, name: str, amount: Decimal) -> None:
        def apply(data: dict) -> dict:
            try:
                current = Decimal(str(data.get(name, "0")))
            except (ArithmeticError, ValueError):
                current = Decimal("0")
            total = (current + amount).quantize(Decimal("0.0000000001"))
            data[name] = float(total)
            return data

        update_json(self.path, {}, apply)

    def observe(self, name: str, value: float) -> None:
        """Record one observation for a latency histogram. Counters are untouched."""
        seconds = float(value)

        def add(data: dict) -> dict:
            histograms = data.get("_histograms")
            if not isinstance(histograms, dict):
                histograms = {}
                data["_histograms"] = histograms
            entry = histograms.get(name)
            if not isinstance(entry, dict):
                entry = {"buckets": {}, "count": 0, "sum": 0.0}
                histograms[name] = entry
            buckets = entry.get("buckets")
            if not isinstance(buckets, dict):
                buckets = {}
                entry["buckets"] = buckets
            entry["count"] = int(entry.get("count") or 0) + 1
            entry["sum"] = float(entry.get("sum") or 0.0) + seconds
            for bound, label in LLM_LATENCY_BUCKETS:
                current = int(buckets.get(label) or 0)
                if seconds <= bound:
                    current += 1
                buckets[label] = current
            return data

        update_json(self.path, {}, add)

    def snapshot(self) -> dict[str, int | float]:
        ints, floats = _numbers(read_json(self.path, {}))
        merged: dict[str, int | float] = {}
        merged.update(ints)
        merged.update(floats)
        return dict(sorted(merged.items()))

    def render_prometheus(self) -> str:
        raw = read_json(self.path, {})
        if not isinstance(raw, dict):
            raw = {}
        ints, floats = _numbers(raw)
        lines: list[str] = []
        lines.extend(_counter_lines(ints))
        lines.extend(_float_counter_lines(floats))
        lines.extend(_tool_lines(ints))
        lines.extend(_histogram_lines(raw.get("_histograms")))
        return "\n".join(lines) + "\n"


def content_type() -> str:
    return _CONTENT_TYPE


def _numbers(raw: object) -> tuple[dict[str, int], dict[str, float]]:
    """Split stored values into integer counters and float counters.

    Keys starting with `_` are internal (histogram state) and are skipped.
    Strings and other non-numeric values are skipped with a debug log.
    """
    if not isinstance(raw, dict):
        return {}, {}
    ints: dict[str, int] = {}
    floats: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _LOG.debug("skipping non-numeric metric %s (%s)", key, type(value).__name__)
            continue
        if isinstance(value, int):
            ints[key] = value
        else:
            floats[key] = float(value)
    return dict(sorted(ints.items())), dict(sorted(floats.items()))


def _counter_lines(counters: dict[str, int]) -> list[str]:
    known = {name for name, _help in _COUNTER_HELP}
    lines: list[str] = []
    for name, help_text in _COUNTER_HELP:
        lines.extend(_counter_family(_metric_name(name), help_text, counters.get(name, 0)))
    extras = sorted(key for key in counters if key not in known and not key.startswith("tool."))
    for name in extras:
        lines.extend(_counter_family(_metric_name(name), f"Holdfast counter {name}.", counters[name]))
    return lines


def _float_counter_lines(counters: dict[str, float]) -> list[str]:
    lines: list[str] = []
    for name in sorted(counters):
        help_text = _FLOAT_HELP.get(name, f"Holdfast counter {name}.")
        metric = _metric_name(name)
        lines.extend(
            [
                f"# HELP {metric} {_escape_help(help_text)}",
                f"# TYPE {metric} counter",
                f"{metric} {_format_number(counters[name])}",
                "",
            ]
        )
    return lines


def _tool_lines(counters: dict[str, int]) -> list[str]:
    values = {tool: 0 for tool in _TOOLS}
    for key, value in counters.items():
        if key.startswith("tool.") and key != "tool.":
            values[key.removeprefix("tool.")] = value
    lines = [
        "# HELP holdfast_tool_calls_total MCP tool calls.",
        "# TYPE holdfast_tool_calls_total counter",
    ]
    for tool in sorted(values):
        lines.append(f'holdfast_tool_calls_total{{tool="{_escape_label(tool)}"}} {values[tool]}')
    lines.append("")
    return lines


def _histogram_lines(raw: object) -> list[str]:
    stored = raw if isinstance(raw, dict) else {}
    known = {name for name, _help in _HISTOGRAMS}
    lines: list[str] = []
    families = list(_HISTOGRAMS)
    for name in sorted(stored):
        if isinstance(name, str) and name not in known:
            families.append((name, f"Holdfast histogram {name}."))
    for name, help_text in families:
        entry = stored.get(name)
        lines.extend(_one_histogram(_histogram_name(name), help_text, entry if isinstance(entry, dict) else {}))
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _one_histogram(metric: str, help_text: str, entry: dict) -> list[str]:
    buckets = entry.get("buckets") if isinstance(entry.get("buckets"), dict) else {}
    count = int(entry.get("count") or 0)
    total = float(entry.get("sum") or 0.0)
    lines = [
        f"# HELP {metric} {_escape_help(help_text)}",
        f"# TYPE {metric} histogram",
    ]
    for _bound, label in LLM_LATENCY_BUCKETS:
        lines.append(f'{metric}_bucket{{le="{label}"}} {int(buckets.get(label) or 0)}')
    lines.append(f'{metric}_bucket{{le="+Inf"}} {count}')
    lines.append(f"{metric}_sum {_format_number(total)}")
    lines.append(f"{metric}_count {count}")
    lines.append("")
    return lines


def _counter_family(metric: str, help_text: str, value: int) -> list[str]:
    return [
        f"# HELP {metric} {_escape_help(help_text)}",
        f"# TYPE {metric} counter",
        f"{metric} {value}",
        "",
    ]


def _metric_name(name: str) -> str:
    slug = _slug(name)
    if slug.startswith("holdfast_"):
        slug = slug.removeprefix("holdfast_") or "unknown"
    return f"holdfast_{slug}_total"


def _histogram_name(name: str) -> str:
    slug = _slug(name)
    if slug.startswith("holdfast_"):
        return slug
    return f"holdfast_{slug}"


def _slug(name: str) -> str:
    chars: list[str] = []
    for char in name.strip().lower():
        chars.append(char if char.isascii() and (char.isalnum() or char == "_") else "_")
    slug = "".join(chars).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    if not slug:
        slug = "unknown"
    if slug[0].isdigit():
        slug = f"n_{slug}"
    return slug


def _escape_help(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def _escape_label(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_number(value: float) -> str:
    text = format(value, ".9f").rstrip("0").rstrip(".")
    return text if text else "0"
