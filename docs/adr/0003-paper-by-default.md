# ADR 0003: Paper by default, mainnet behind two gates

`config/default.json` sets `venue` to `paper` and `allow_mainnet` to `false`. A test reads that file so a pull request cannot flip the default quietly.

`HLGUARD_VENUE=mainnet` is not enough. `load_settings` also requires `allow_mainnet: true` in the config file and `HLGUARD_ALLOW_MAINNET=1` in the environment. The environment cannot set `allow_mainnet`. Testnet is opt-in via the venue and still goes through `place_order`.

Paper fills never import `hyperliquid` or `eth_account` and never read the wallet key. The key is not passed into the paper trade subprocess.

## Consequences

The demo and the evals have nothing to spend. Turning on mainnet is an explicit edit in two places, plus a key that is not in the repo.
