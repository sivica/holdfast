# ADR 0004: Approval tokens bind the economic plan

The human sees the canonical plan: ticker, side, size in USD, leverage, margin mode, stop, and max loss. Those fields are hashed with SHA-256. The approval service signs `v1.{hash}.{expiry}.{nonce}` with HMAC-SHA256.

`place_order` recomputes the hash from the arguments it received. It does not trust a client-supplied hash. The nonce is stored and rejected on replay. The default TTL is 10 minutes.

The token is not shown in the UI and is not written to the audit log. The log stores the nonce and the plan hash.

The rationale is audit-logged and redacted. It is not part of the hash. The hash covers the fields that can lose money. Prose cannot retarget the order.

Issuing a token for an illegal plan on purpose still fails. `place_order` runs the policy again after the signature check. The eval harness tries to approve blocked plans and must be refused.

## Consequences

A human approval of a $10 plan cannot be replayed as a $10,000 plan. Approving twice with the same token does not fill twice.
