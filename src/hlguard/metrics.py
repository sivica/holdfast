from __future__ import annotations

from pathlib import Path

from hlguard.store import read_json, update_json

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

    def snapshot(self) -> dict[str, int]:
        return _counters(read_json(self.path, {}))

    def render_prometheus(self) -> str:
        raw = read_json(self.path, {})
        if not isinstance(raw, dict):
            raw = {}
        lines: list[str] = []
        lines.extend(_counter_lines(_counters(raw)))
        lines.extend(_tool_lines(_counters(raw)))
        lines.extend(_histogram_lines(raw.get("_histograms")))
        return "\n".join(lines) + "\n"


def content_type() -> str:
    return _CONTENT_TYPE


def _counters(raw: object) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    counters: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        counters[key] = int(value)
    return dict(sorted(counters.items()))


def _counter_lines(counters: dict[str, int]) -> list[str]:
    known = {name for name, _help in _COUNTER_HELP}
    lines: list[str] = []
    for name, help_text in _COUNTER_HELP:
        lines.extend(_counter_family(_metric_name(name), help_text, counters.get(name, 0)))
    extras = sorted(key for key in counters if key not in known and not key.startswith("tool."))
    for name in extras:
        lines.extend(_counter_family(_metric_name(name), f"Holdfast counter {name}.", counters[name]))
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
    return f"holdfast_{_slug(name)}_total"


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
