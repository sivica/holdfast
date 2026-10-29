from __future__ import annotations

import asyncio
import json
import sys
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hlguard.config import find_root
from hlguard.errors import MarketError
from hlguard.redact import redact
from hlguard.runtime import Deps, child_env


def _run(builder):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(builder())
    box: list = []
    error: list = []

    def target() -> None:
        try:
            box.append(asyncio.run(builder()))
        except Exception as exc:  # noqa: BLE001 — re-raised on the caller thread
            error.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if error:
        raise error[0]
    return box[0]


def research(deps: Deps) -> dict:
    return _run(lambda: _research(deps))


def execute(deps: Deps, arguments: dict) -> dict:
    return _run(lambda: _execute(deps, arguments))


def list_tool_names(deps: Deps, scope: str) -> list[str]:
    return _run(lambda: _list_tools(deps, scope))


async def _research(deps: Deps) -> dict:
    async def call(session: ClientSession) -> dict:
        names = sorted(tool.name for tool in (await session.list_tools()).tools)
        markets = _decode(await session.call_tool("list_markets", {}))
        news = _decode(await session.call_tool("get_news", {}))
        account = _decode(await session.call_tool("get_account", {}))
        return {"tools": names, "markets": markets, "news": news, "account": account}

    return await _session(deps, "read", call)


async def _execute(deps: Deps, arguments: dict) -> dict:
    async def call(session: ClientSession) -> dict:
        names = sorted(tool.name for tool in (await session.list_tools()).tools)
        if names != ["place_order"]:
            raise MarketError("trade server tool list is not place_order alone")
        return _decode(await session.call_tool("place_order", arguments))

    return await _session(deps, "trade", call)


async def _list_tools(deps: Deps, scope: str) -> list[str]:
    async def call(session: ClientSession) -> list[str]:
        return sorted(tool.name for tool in (await session.list_tools()).tools)

    return await _session(deps, scope, call)


async def _session(deps: Deps, scope: str, call):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "hlguard.mcp_server",
            "--scope",
            scope,
            "--data-dir",
            str(deps.data_dir),
            "--config",
            str(deps.config_path),
        ],
        env=child_env(scope, deps),
        cwd=str(find_root()),
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await call(session)
    except Exception as exc:
        raise MarketError(f"MCP {scope} server failed: {redact(str(exc))}") from exc


def _decode(result) -> dict:
    if getattr(result, "is_error", False) and not getattr(result, "content", None):
        raise MarketError("MCP tool returned an error")
    content = getattr(result, "content", None) or []
    if not content:
        structured = getattr(result, "structured_content", None)
        if isinstance(structured, dict):
            return structured
        raise MarketError("MCP tool returned no content")
    text = getattr(content[0], "text", None)
    if text is None:
        raise MarketError("MCP tool returned non-text content")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise MarketError("MCP tool returned unexpected JSON")
    return payload
