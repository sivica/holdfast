# ADR 0002: Read and trade are different MCP servers

The research step starts `python -m hlguard.mcp_server --scope read`. That process registers `list_markets`, `get_market`, `get_news`, and `get_account`. It does not register `place_order`.

Its environment is an allowlist. It does not receive `HLGUARD_APPROVAL_SECRET`, `HYPERLIQUID_API_WALLET_KEY`, or `HLGUARD_ACCOUNT_ADDRESS`.

After a human approves, a second process starts with `--scope trade`. That process registers `place_order` only. In paper mode it receives the approval secret and still does not receive the trading key.

There is no scope that exposes both.

## Consequences

A prompt cannot call a tool the research server does not have. A stolen research-process environment cannot mint an approval token or sign an exchange request.
