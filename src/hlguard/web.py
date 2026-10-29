from __future__ import annotations

import hmac
import os
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from hlguard.agent import run_cycle
from hlguard.errors import ApprovalError, HoldfastError, PolicyError
from hlguard.evalrunner import load_scenarios
from hlguard.llm import GuardedMock, GullibleMock
from hlguard.models import NewsItem, Snapshot
from hlguard.news import load_news
from hlguard.redact import redact
from hlguard.runtime import Deps, list_proposals
from hlguard.web_view import render_desk

LIVE_NEWS = [
    NewsItem(
        source="holdfast",
        title="No feed",
        text="Live cycle has no news vendor configured. Absence of news is not a signal.",
    )
]


def create_app(deps: Deps) -> FastAPI:
    app = FastAPI(title="Holdfast", docs_url=None, redoc_url=None)
    app.state.deps = deps

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "service": "holdfast", "venue": deps.settings.venue}

    @app.get("/metrics")
    def metrics() -> dict:
        return deps.metrics.snapshot()

    @app.get("/audit")
    def audit() -> dict:
        events = deps.audit.read()
        return {"ok": deps.audit.verify(), "events": events[-50:]}

    @app.get("/", response_class=HTMLResponse)
    def desk(request: Request) -> HTMLResponse:
        token = _csrf_token(request)
        response = HTMLResponse(render_desk(_context(deps, token, request)))
        response.set_cookie("hf_csrf", token, httponly=True, samesite="lax", path="/")
        return response

    @app.post("/cycle")
    async def cycle(request: Request) -> RedirectResponse:
        form = await request.form()
        if not _csrf_ok(request, str(form.get("form_token") or "")):
            return _redirect("bad_token")
        source = str(form.get("source") or "")
        scenario_id = str(form.get("scenario") or "")
        try:
            if source == "live":
                _run_live(deps)
            else:
                known = {item["id"] for item in load_scenarios()}
                if scenario_id not in known:
                    return _redirect("unknown_scenario")
                _run_scenario(deps, scenario_id)
        except HoldfastError as exc:
            _flash(deps, redact(str(exc)))
            return _redirect("cycle_failed")
        latest = _latest(deps)
        status = latest.get("status") if latest else "cycle_failed"
        known = {"filled", "rejected", "blocked", "hold", "pending", "failed"}
        return _redirect(status if status in known else "cycle_failed")

    @app.post("/proposals/{proposal_id}/approve")
    async def approve(proposal_id: str, request: Request) -> RedirectResponse:
        return await _decision(deps, request, proposal_id, "approve")

    @app.post("/proposals/{proposal_id}/reject")
    async def reject(proposal_id: str, request: Request) -> RedirectResponse:
        return await _decision(deps, request, proposal_id, "reject")

    return app


def _run_scenario(deps: Deps, scenario_id: str) -> dict:
    scenario = next(item for item in load_scenarios() if item["id"] == scenario_id)
    previous = os.environ.get("HLGUARD_MARKET_SOURCE")
    os.environ["HLGUARD_MARKET_SOURCE"] = "file"
    try:
        provider = GullibleMock() if scenario["llm"] == "gullible" else GuardedMock()
        return run_cycle(
            deps,
            provider=provider,
            snapshots=[Snapshot.from_dict(item) for item in scenario["markets"]],
            news=[NewsItem(source=item["source"], title=item["title"], text=item["text"]) for item in scenario["news"]],
            focus=scenario["focus"],
            llm_name=scenario["llm"],
        )
    finally:
        if previous is None:
            os.environ.pop("HLGUARD_MARKET_SOURCE", None)
        else:
            os.environ["HLGUARD_MARKET_SOURCE"] = previous


def _run_live(deps: Deps) -> dict:
    previous = os.environ.get("HLGUARD_MARKET_SOURCE")
    os.environ["HLGUARD_MARKET_SOURCE"] = "live"
    try:
        return run_cycle(
            deps,
            provider=GuardedMock(),
            snapshots=[],
            news=LIVE_NEWS,
            focus="BTC",
            llm_name="mock",
        )
    finally:
        if previous is None:
            os.environ.pop("HLGUARD_MARKET_SOURCE", None)
        else:
            os.environ["HLGUARD_MARKET_SOURCE"] = previous


async def _decision(deps: Deps, request: Request, proposal_id: str, decision: str) -> RedirectResponse:
    from hlguard.agent import decide

    form = await request.form()
    if not _csrf_ok(request, str(form.get("form_token") or "")):
        return _redirect("bad_token")
    try:
        record = decide(deps, proposal_id, decision, "web")
    except ApprovalError as exc:
        return _redirect("not_pending" if exc.code == "not_pending" else "failed")
    except PolicyError:
        return _redirect("blocked")
    return _redirect(str(record.get("status") or "failed"))


def _context(deps: Deps, token: str, request: Request) -> dict:
    latest = _latest(deps)
    markets = _markets(deps)
    news = [item.to_dict() for item in load_news(deps.data_dir / "news.json")]
    account = deps.broker.view()
    from hlguard.models import q_usd
    from hlguard.policy import risk_used

    return {
        "audit_ok": deps.audit.verify(),
        "audit_tail": list(reversed(deps.audit.read()[-8:])),
        "daily_cap": format(deps.settings.daily_loss_cap_usd, "f"),
        "daily_used": format(q_usd(risk_used(account)), "f"),
        "detail": _consume_flash(deps),
        "error": request.query_params.get("error"),
        "history": list(reversed(list_proposals(deps)[-6:])),
        "markets": markets,
        "max_notional": format(deps.settings.max_notional_usd, "f"),
        "metrics": deps.metrics.snapshot(),
        "news": news,
        "notice": request.query_params.get("notice"),
        "proposal": latest,
        "token": token,
        "venue": deps.settings.venue,
    }


def _markets(deps: Deps) -> list[dict]:
    path = deps.data_dir / "market.json"
    if not path.exists():
        return []
    from hlguard.market import read_market

    try:
        allow = set(deps.settings.allowlist)
        return [snapshot.to_dict() for snapshot in read_market(path) if snapshot.ticker in allow]
    except Exception:
        return []


def _latest(deps: Deps) -> dict | None:
    items = list_proposals(deps)
    return items[-1] if items else None


def _redirect(code: str) -> RedirectResponse:
    return RedirectResponse(f"/?notice={code}", status_code=303)


def _csrf_token(request: Request) -> str:
    current = request.cookies.get("hf_csrf", "")
    if len(current) >= 16:
        return current
    return secrets.token_urlsafe(24)


def _csrf_ok(request: Request, form_token: str) -> bool:
    cookie = request.cookies.get("hf_csrf", "")
    if not cookie or not form_token or len(cookie) != len(form_token):
        return False
    return hmac.compare_digest(cookie, form_token)


def _flash(deps: Deps, message: str) -> None:
    path = deps.data_dir / "flash.txt"
    path.write_text(redact(message))


def _consume_flash(deps: Deps) -> str:
    path = deps.data_dir / "flash.txt"
    if not path.exists():
        return ""
    text = redact(path.read_text())
    path.unlink(missing_ok=True)
    return text


def demo_cycle(deps: Deps) -> None:
    if any(item.get("status") == "pending" for item in list_proposals(deps)):
        return
    _run_scenario(deps, "btc_short_approved")
