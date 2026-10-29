from __future__ import annotations

import re
from html import escape

_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

_NOTICES = {
    "filled": "Approved. The paper order filled after the token was checked against this plan hash.",
    "rejected": "Rejected. No order was sent.",
    "blocked": "Policy blocked this plan. No approval token was issued.",
    "hold": "The analyst returned a hold. Nothing is waiting for approval.",
    "pending": "A plan is waiting. Nothing executes until you approve this exact plan.",
    "failed": "The order was refused at execution. The ledger was not filled.",
    "bad_token": "The form token did not match. Nothing was changed.",
    "not_pending": "That plan is not waiting for approval.",
    "unknown_scenario": "Unknown scenario. The desk only runs the fixed set.",
    "cycle_failed": "The cycle failed before a plan was stored.",
}

_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Holdfast desk</title>
<style>
:root {
  --bg: #101410;
  --card: #171d18;
  --ink: #e7f0e4;
  --muted: #93a396;
  --line: #2c382f;
  --acid: #dff25a;
  --acid-ink: #1a1f0c;
  --coral: #ff6a45;
  --mint: #8ee0b5;
  --warn: #f0c36a;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  line-height: 1.45;
}
header, main { width: min(1080px, calc(100% - 32px)); margin: 0 auto; }
header {
  padding: 28px 0 12px; display: flex; justify-content: space-between;
  gap: 16px; flex-wrap: wrap; align-items: flex-end;
}
.mark { font-family: Georgia, "Iowan Old Style", serif; font-size: 32px; letter-spacing: 0.08em; margin: 0; }
.sub { color: var(--muted); margin: 4px 0 0; }
.badge {
  border: 1px solid var(--acid); color: var(--acid); padding: 4px 8px;
  font-family: ui-monospace, monospace; font-size: 12px; letter-spacing: 0.12em;
}
.limits { display: flex; gap: 8px; flex-wrap: wrap; margin: 8px 0 0; }
.limits span { border: 1px solid var(--line); padding: 4px 8px; color: var(--muted); font-size: 13px; }
.layout { display: grid; grid-template-columns: 1.25fr 0.75fr; gap: 16px; padding-bottom: 40px; }
.card { background: var(--card); border: 1px solid var(--line); padding: 16px; }
h2 { font-family: Georgia, serif; font-weight: 500; font-size: 20px; margin: 0 0 12px; }
.banner { padding: 10px 12px; margin: 0 0 12px; border: 1px solid var(--line); }
.banner.good { border-color: var(--mint); }
.banner.bad { border-color: var(--coral); }
.banner.warn { border-color: var(--warn); }
.kvs {
  display: grid; grid-template-columns: 140px 1fr; gap: 6px 10px;
  font-family: ui-monospace, monospace; font-size: 14px;
}
.kvs dt { color: var(--muted); }
.side-long { color: var(--mint); }
.side-short { color: #ffb4a2; }
pre {
  white-space: pre-wrap; word-break: break-word; font-family: ui-monospace, monospace;
  font-size: 13px; background: #101410; padding: 10px; border: 1px solid var(--line);
}
.actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 12px; }
button { font: inherit; border: 0; padding: 10px 14px; cursor: pointer; }
button.approve { background: var(--acid); color: var(--acid-ink); }
button.reject, button.secondary { background: transparent; color: var(--ink); border: 1px solid var(--line); }
button.drill { background: transparent; color: var(--coral); border: 1px solid var(--coral); }
.untrusted { border-style: dashed; }
.tag { font-family: ui-monospace, monospace; font-size: 11px; letter-spacing: 0.14em; color: var(--warn); }
table {
  width: 100%; border-collapse: collapse; font-variant-numeric: tabular-nums;
  font-family: ui-monospace, monospace; font-size: 13px;
}
th, td { text-align: left; padding: 6px 4px; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 500; }
.up { color: var(--mint); }
.down { color: #ffb4a2; }
ul.reasons { margin: 8px 0; padding-left: 18px; }
footer { color: var(--muted); font-size: 13px; padding-bottom: 28px; }
@media (max-width: 720px) {
  .layout { grid-template-columns: 1fr; }
  .actions { flex-direction: column; }
  .actions button { width: 100%; }
  .kvs { grid-template-columns: 1fr; }
}
</style>
</head>
<body>
<!--BODY-->
</body>
</html>
"""


def render_desk(context: dict) -> str:
    return _PAGE.replace("<!--BODY-->", _body(context))


def _body(context: dict) -> str:
    token = escape(str(context.get("token") or ""))
    proposal = context.get("proposal")
    parts = [
        "<header>",
        "<div>",
        "<p class='mark'>HOLDFAST</p>",
        "<p class='sub'>Guarded paper desk · Hyperliquid perps and HIP-3 xyz</p>",
        "<div class='limits'>",
        f"<span>max notional ${escape(str(context.get('max_notional')))}</span>",
        "<span>1x isolated only</span>",
        "<span>stop required</span>",
        f"<span>daily loss cap ${escape(str(context.get('daily_cap')))}</span>",
        "</div>",
        "</div>",
        f"<div class='badge'>{escape(str(context.get('venue') or 'paper')).upper()}</div>",
        "</header>",
        "<main class='layout'>",
        "<section>",
        _notice(context),
        _plan_card(proposal, token),
        _actions(token),
        "</section>",
        "<aside>",
        _markets(context.get("markets") or []),
        _news(context.get("news") or [], proposal),
        _audit(context),
        "</aside>",
        "</main>",
        "<footer style='width:min(1080px,calc(100% - 32px));margin:0 auto'>",
        "Research calls a read-scoped MCP server. place_order lives on a second server and "
        "refuses a token that does not match the plan hash. News in the dashed box is untrusted.",
        "</footer>",
    ]
    return "\n".join(parts)


def _notice(context: dict) -> str:
    chunks = []
    code = context.get("notice")
    if code in _NOTICES:
        kind = "bad" if code in {"blocked", "failed", "bad_token", "cycle_failed"} else "good"
        chunks.append(f"<p class='banner {kind}'>{escape(_NOTICES[code])}</p>")
    error = context.get("error")
    if error in _NOTICES and error != code:
        chunks.append(f"<p class='banner bad'>{escape(_NOTICES[error])}</p>")
    detail = context.get("detail")
    if detail:
        chunks.append(f"<p class='banner warn'>{escape(str(detail))}</p>")
    return "\n".join(chunks)


def _plan_card(proposal: dict | None, token: str) -> str:
    if not proposal:
        return (
            "<article class='card' data-status='empty' data-can-approve='false'>"
            "<h2>No plan yet</h2>"
            "<p>Propose a trade from the fixed desk snapshot, or run the injection drill. "
            "The drill uses a model that obeys the malicious news. The desk should block it.</p>"
            "</article>"
        )
    status = str(proposal.get("status") or "")
    can = "true" if status == "pending" and _ID.match(str(proposal.get("id") or "")) else "false"
    side = str((proposal.get("plan") or {}).get("side") or "")
    side_class = "side-long" if side == "long" else "side-short" if side == "short" else ""
    plan = proposal.get("plan") or {}
    rows = [
        ("status", status),
        ("ticker", proposal.get("ticker")),
        ("side", side or "—"),
        ("size", None if not plan else plan.get("size_usd")),
        ("leverage", None if not plan else plan.get("leverage")),
        ("margin", None if not plan else plan.get("margin_mode")),
        ("stop", None if not plan else plan.get("stop_loss")),
        ("max loss", None if not plan else plan.get("max_loss_usd")),
        ("hash", proposal.get("plan_hash") or "—"),
        ("llm", proposal.get("llm") or "—"),
        ("served by", proposal.get("served_by") or "—"),
        ("tokens", _token_line(proposal)),
        ("cost usd", "omitted" if proposal.get("cost_usd") is None else proposal.get("cost_usd")),
    ]
    if proposal.get("fallback_reason"):
        rows.append(("fallback", proposal.get("fallback_reason")))
    body = ["<dl class='kvs'>"]
    for label, value in rows:
        shown = "—" if value is None else str(value)
        klass = side_class if label == "side" else ""
        body.append(f"<dt>{escape(label)}</dt><dd class='{klass}'>{escape(shown)}</dd>")
    body.append("</dl>")
    if proposal.get("reasons"):
        body.append("<ul class='reasons'>")
        for reason in proposal["reasons"]:
            body.append(f"<li>{escape(str(reason))}</li>")
        body.append("</ul>")
    rationale = proposal.get("rationale") or ""
    if rationale:
        label = "Model output, stored as data" if status == "blocked" else "Rationale"
        body.append(f"<p class='tag'>{escape(label.upper())}</p>")
        body.append(f"<pre>{escape(rationale)}</pre>")
    fill = proposal.get("fill")
    if fill:
        body.append("<p class='tag'>PAPER FILL</p>")
        body.append(f"<pre>{escape(_fill_text(fill))}</pre>")
    actions = ""
    if can == "true":
        pid = escape(str(proposal["id"]))
        actions = (
            "<div class='actions'>"
            f"<form method='post' action='/proposals/{pid}/approve'>"
            f"<input type='hidden' name='form_token' value='{token}'>"
            "<button class='approve' id='approve' type='submit'>Approve plan</button>"
            "</form>"
            f"<form method='post' action='/proposals/{pid}/reject'>"
            f"<input type='hidden' name='form_token' value='{token}'>"
            "<button class='reject' id='reject' type='submit'>Reject</button>"
            "</form>"
            "</div>"
        )
    suspected = ""
    if proposal.get("injection_suspected"):
        labels = ", ".join(proposal.get("injection_labels") or [])
        suspected = (
            "<p class='banner warn'>Injection patterns were flagged "
            f"({escape(labels)}). The flag is only a signal. Limits are enforced in code.</p>"
        )
    return (
        f"<article class='card' data-status='{escape(status)}' data-can-approve='{can}' "
        f"data-reasons='{escape(' '.join(proposal.get('reasons') or []))}'>"
        f"<h2>Plan</h2>{suspected}{''.join(body)}{actions}</article>"
    )


def _actions(token: str) -> str:
    hidden = f"<input type='hidden' name='form_token' value='{token}'>"
    return (
        "<div class='card' style='margin-top:16px'>"
        "<h2>Run</h2>"
        "<div class='actions'>"
        f"<form method='post' action='/cycle'>{hidden}"
        "<input type='hidden' name='scenario' value='btc_short_approved'>"
        "<button class='secondary' id='propose' type='submit'>Propose a trade</button></form>"
        f"<form method='post' action='/cycle'>{hidden}"
        "<input type='hidden' name='scenario' value='injection_notional'>"
        "<button class='drill' id='injection-drill' type='submit'>Run injection drill</button></form>"
        f"<form method='post' action='/cycle'>{hidden}"
        "<input type='hidden' name='source' value='live'>"
        "<button class='secondary' id='refresh-live' type='submit'>Refresh live markets</button></form>"
        "</div></div>"
    )


def _markets(markets: list[dict]) -> str:
    rows = [
        "<div class='card'><h2>Markets</h2><table><thead><tr>",
        "<th>Ticker</th><th>Mark</th><th>Prev day</th><th>Change</th><th>Funding</th>",
        "</tr></thead><tbody>",
    ]
    if not markets:
        rows.append("<tr><td colspan='5'>No snapshot loaded.</td></tr>")
    for market in markets:
        change = str(market.get("change_pct") or "")
        klass = "down"
        if change and not change.startswith("-") and change != "0.00":
            klass = "up"
        rows.append(
            "<tr>"
            f"<td>{escape(str(market.get('ticker')))}</td>"
            f"<td>{escape(str(market.get('mark')))}</td>"
            f"<td>{escape(str(market.get('prev_day')))}</td>"
            f"<td class='{klass}'>{escape(change)}%</td>"
            f"<td>{escape(str(market.get('funding')))}</td>"
            "</tr>"
        )
    rows.append("</tbody></table></div>")
    return "".join(rows)


def _news(items: list[dict], proposal: dict | None) -> str:
    chunks = [
        "<div class='card untrusted' style='margin-top:16px'>",
        "<p class='tag'>UNTRUSTED NEWS</p>",
        "<h2>Context</h2>",
    ]
    if not items:
        chunks.append("<p>No news loaded.</p>")
    for item in items:
        chunks.append(
            f"<p><strong>{escape(str(item.get('title') or ''))}</strong> "
            f"<span class='tag'>{escape(str(item.get('source') or ''))}</span><br>"
            f"{escape(str(item.get('text') or ''))}</p>"
        )
    if proposal and proposal.get("read_tools"):
        tools = ", ".join(proposal["read_tools"])
        chunks.append(f"<p class='tag'>READ TOOLS {escape(tools)}</p>")
    chunks.append("</div>")
    return "".join(chunks)


def _audit(context: dict) -> str:
    ok = "ok" if context.get("audit_ok") else "BROKEN"
    lines = ["<div class='card' style='margin-top:16px'>", f"<h2>Audit {escape(ok)}</h2>", "<ul class='reasons'>"]
    tail = context.get("audit_tail") or []
    if not tail:
        lines.append("<li>No events yet.</li>")
    for event in tail:
        lines.append(
            f"<li>{escape(str(event.get('event')))} {escape(str(event.get('status') or event.get('ticker') or ''))} "
            f"<span class='tag'>{escape(str(event.get('hash') or '')[:8])}</span></li>"
        )
    metrics = context.get("metrics") or {}
    if metrics:
        rendered = " ".join(f"{escape(str(key))}={escape(str(value))}" for key, value in metrics.items())
        lines.append(f"</ul><p class='tag'>{rendered}</p></div>")
    else:
        lines.append("</ul></div>")
    return "".join(lines)


def _token_line(proposal: dict) -> str:
    incoming = proposal.get("input_tokens")
    outgoing = proposal.get("output_tokens")
    if incoming is None and outgoing is None:
        return "—"
    return f"{incoming} in / {outgoing} out"


def _fill_text(fill: dict) -> str:
    return (
        f"{fill.get('side')} {fill.get('ticker')} {fill.get('size_coin')} @ {fill.get('fill_px')}\n"
        f"notional ${fill.get('notional_usd')}  stop {fill.get('stop_loss')}\n"
        f"order {fill.get('order_id')}\nstop order {fill.get('stop_order_id')}"
    )
