# ADR 0001: Risk limits live in code

The analyst is told the limits. That reminder is not the control.

`policy.evaluate` is called before a plan can become pending, and `place_order` calls it again after the approval token checks out. Leverage is the constant `1`. Margin mode is the constant `isolated`. The notional cap, per-trade max loss, daily loss cap, and allowlist come from the config file. There is no environment variable that widens them.

A test double that obeys injected news is part of the eval suite. If the only defense were the prompt, that scenario would fill.

## Consequences

Changing a limit is a reviewed diff. The eval baseline stores the exact deny reasons, so deleting a check fails CI even when the order is still blocked for a different reason.
