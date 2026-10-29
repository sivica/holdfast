from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from hlguard import __version__
from hlguard.agent import decide, interpret_decision, run_cycle
from hlguard.errors import HoldfastError
from hlguard.evalrunner import (
    baseline_path,
    compare_baseline,
    load_scenarios,
    render_report,
    run_suite,
)
from hlguard.llm import build_provider
from hlguard.models import NewsItem, Snapshot
from hlguard.redact import redact
from hlguard.runtime import build_deps


def main(argv: list[str] | None = None) -> None:
    sys.exit(run(argv))


def run(argv: list[str] | None = None) -> int:
    _configure_logging()
    parser = argparse.ArgumentParser(prog="holdfast", description="Guarded Hyperliquid paper desk")
    parser.add_argument("--version", action="version", version=f"holdfast {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    run_parser = sub.add_parser("run", help="Propose one paper trade and ask for approval")
    _add_common(run_parser)
    run_parser.add_argument("--scenario", default="btc_short_approved")
    run_parser.add_argument("--live", action="store_true")
    run_parser.add_argument("--ticker", default="BTC")
    run_parser.add_argument("--llm", default=None)
    run_parser.add_argument("--decision", choices=["approve", "reject"], default=None)

    serve_parser = sub.add_parser("serve", help="Approval desk")
    _add_common(serve_parser)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8741)
    serve_parser.add_argument("--demo", action="store_true")

    mcp_parser = sub.add_parser("mcp", help="MCP server (read or trade scope)")
    mcp_parser.add_argument("--scope", choices=["read", "trade"], required=True)
    mcp_parser.add_argument("--data-dir", required=True)
    mcp_parser.add_argument("--config", required=True)

    eval_parser = sub.add_parser("eval", help="Run the offline scenario suite")
    eval_parser.add_argument("--config", default=None)
    eval_parser.add_argument("--fail-under-baseline", action="store_true")
    eval_parser.add_argument("--write-baseline", action="store_true")

    sub.add_parser("audit-verify", help="Check the audit hash chain").add_argument("--data-dir", default=None)

    args = parser.parse_args(argv)
    try:
        if args.cmd == "run":
            return _cmd_run(args)
        if args.cmd == "serve":
            return _cmd_serve(args)
        if args.cmd == "mcp":
            from hlguard.mcp_server import main as mcp_main

            return mcp_main(["--scope", args.scope, "--data-dir", args.data_dir, "--config", args.config])
        if args.cmd == "eval":
            return _cmd_eval(args)
        if args.cmd == "audit-verify":
            return _cmd_audit(args)
    except HoldfastError as exc:
        print(redact(str(exc)), file=sys.stderr)
        return 1
    return 2


def _cmd_run(args: argparse.Namespace) -> int:
    if args.live and args.scenario and args.scenario != "btc_short_approved":
        print("Use either --live or --scenario.", file=sys.stderr)
        return 2
    deps = build_deps(_path(args.config), _path(args.data_dir))
    if args.live:
        llm_name = _selected_llm(args.llm)
        if llm_name.strip().lower() == "gullible":
            print(
                "The gullible model is an offline test double and cannot be used with --live.",
                file=sys.stderr,
            )
            return 2
        os.environ["HLGUARD_MARKET_SOURCE"] = "live"
        provider = build_provider(llm_name)
        record = run_cycle(
            deps,
            provider=provider,
            snapshots=[],
            news=[
                NewsItem(
                    source="holdfast",
                    title="No feed",
                    text="Live cycle has no news vendor configured. Absence of news is not a signal.",
                )
            ],
            focus=args.ticker,
            llm_name=llm_name,
        )
    else:
        os.environ["HLGUARD_MARKET_SOURCE"] = "file"
        scenario = _scenario(args.scenario)
        llm_name = _selected_llm(args.llm, scenario["llm"])
        record = run_cycle(
            deps,
            provider=build_provider(llm_name),
            snapshots=[Snapshot.from_dict(item) for item in scenario["markets"]],
            news=[NewsItem(**item) for item in scenario["news"]],
            focus=scenario["focus"],
            llm_name=llm_name,
        )
    _print_record(record)
    if record["status"] == "pending":
        decision = args.decision or interpret_decision(_prompt())
        record = decide(deps, record["id"], decision, "cli")
        _print_record(record)
    _print_footer(deps)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hlguard.web import create_app, demo_cycle

    deps = build_deps(_path(args.config), _path(args.data_dir))
    if args.demo:
        try:
            demo_cycle(deps)
        except HoldfastError as exc:
            (deps.data_dir / "flash.txt").write_text(redact(str(exc)))
    uvicorn.run(create_app(deps), host=args.host, port=args.port, log_level="info")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    report = run_suite(_path(args.config))
    sys.stdout.write(render_report(report))
    if args.write_baseline:
        if report["policy_compliance"] != 1.0:
            print("refusing to write a baseline below 100% policy compliance", file=sys.stderr)
            return 1
        path = baseline_path()
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        print(f"wrote {path}")
        return 0
    if args.fail_under_baseline:
        mismatch = compare_baseline(report)
        if mismatch:
            print(mismatch, file=sys.stderr)
            print(json.dumps(report, indent=2, sort_keys=True), file=sys.stderr)
            return 1
    return 0 if report["policy_compliance"] == 1.0 else 1


def _cmd_audit(args: argparse.Namespace) -> int:
    deps = build_deps(None, _path(args.data_dir))
    ok = deps.audit.verify()
    print(f"events {len(deps.audit.read())} chain {'ok' if ok else 'BROKEN'}")
    return 0 if ok else 1


def _print_record(record: dict) -> None:
    status = record.get("status")
    print(f"status     {status}")
    print(f"ticker     {record.get('ticker')}")
    print(f"llm        {record.get('llm')}")
    if record.get("served_by"):
        print(f"served by  {record.get('served_by')}")
    if record.get("fallback_reason"):
        print(f"fallback   {record.get('fallback_reason')}")
    if record.get("input_tokens") is not None:
        print(f"tokens     {record.get('input_tokens')} in / {record.get('output_tokens')} out")
    if "cost_usd" in record:
        cost = record.get("cost_usd")
        print(f"cost usd   {'omitted' if cost is None else cost}")
    if record.get("injection_suspected"):
        print(f"injection  suspected ({', '.join(record.get('injection_labels') or [])})")
    if record.get("read_tools"):
        print(f"read tools {', '.join(record['read_tools'])}")
    plan = record.get("plan") or {}
    if plan:
        print(json.dumps(plan, indent=2, sort_keys=True))
    if record.get("plan_hash"):
        print(f"plan hash  {record['plan_hash']}")
    if record.get("reasons"):
        print("reasons")
        for reason in record["reasons"]:
            print(f"  - {reason}")
    if record.get("rationale"):
        print(record["rationale"])
    fill = record.get("fill")
    if fill:
        print(
            f"Paper fill  {fill.get('side')} {fill.get('ticker')} "
            f"{fill.get('size_coin')} @ {fill.get('fill_px')}  stop {fill.get('stop_loss')}"
        )
    if status == "blocked":
        print("BLOCKED  policy refused this plan before any approval token was issued")
        print("No order was sent.")


def _print_footer(deps) -> None:
    print("metrics")
    for key, value in deps.metrics.snapshot().items():
        print(f"  {key} {_format_metric(key, value)}")
    print(f"audit chain {'ok' if deps.audit.verify() else 'BROKEN'}  ({deps.audit.path})")


def _format_metric(key: str, value: object) -> str:
    if key == "llm_cost_usd":
        text = f"{float(value):.10f}".rstrip("0").rstrip(".")
        return text or "0"
    return str(value)


def _selected_llm(explicit: str | None, scenario: str | None = None) -> str:
    """Scenario runs: flag, then the scenario's model, then HLGUARD_LLM. Live runs skip the scenario."""
    if explicit and explicit.strip():
        return explicit.strip()
    if scenario and str(scenario).strip():
        return str(scenario).strip()
    env = os.environ.get("HLGUARD_LLM", "").strip()
    return env or "mock"


def _prompt() -> str | None:
    try:
        return input("Approve this exact plan? [approve/reject] ")
    except EOFError:
        print("\nNo decision on stdin. Rejecting.")
        return None


def _scenario(scenario_id: str) -> dict:
    for scenario in load_scenarios():
        if scenario["id"] == scenario_id:
            return scenario
    raise HoldfastError(f"unknown scenario: {scenario_id}")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=None)
    parser.add_argument("--data-dir", default=None)


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _configure_logging() -> None:
    class _Redact(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            record.msg = redact(record.getMessage())
            record.args = ()
            return True

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(message)s")
    logging.getLogger().addFilter(_Redact())


if __name__ == "__main__":
    main()
