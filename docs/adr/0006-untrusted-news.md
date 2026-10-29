# ADR 0006: News is an untrusted channel

`get_news` marks every item `trust: untrusted`. The prompt wraps the text in `<untrusted_news>`. The desk renders it in a dashed box and HTML-escapes it.

A detector flags phrases such as "ignore limits", "you are now", and "stop is optional". The flag is a span attribute and a banner. It does not allow or deny an order. The gullible-model scenario is flagged and still blocked. The guarded-model scenario is flagged and still proposes a $10 plan.

Tool spans do not copy the news body. Audit rationales pass through redaction for private keys, the approval secret, and the account address.

## Consequences

The interesting failure is a model that obeys the news. The suite includes that failure. The control is the policy code and the missing trade tool, not the detector.
