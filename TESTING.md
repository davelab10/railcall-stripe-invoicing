# Testing report — dave/stripe-invoicing v1.3.0

Target runtime: RailCall Station v0.66.

Demo video: https://youtu.be/C6INOasnOsM

This report summarizes the completed module verification. Fixture evidence, policy refusal, and live-provider evidence remain distinct; fixture output is never presented as provider success.

## Final status

| Check | Result |
|---|---|
| Module identity | `dave/stripe-invoicing` v1.3.0 |
| Command registration | 29/29 resolved |
| Classification | 16 `read`; 13 `write_requires_approval` |
| Module regression | 17/17 PASS |
| Semantic Firewall | 29/29 PASS |
| Incremental/schedulable contract | PASS |
| Signature tree v2 | PASS |
| Station v0.66 compatibility | PASS; no module migration required |
| Secret-pattern leakage in audited receipts | 0 |

All 29 commands declare `receipt_required: true`. The signed installed module and project source matched during the completed runtime step.

## Incremental `invoice_list`

The existing `stripe.billing.invoice_list` supports manual reads and Station-managed incremental execution. No parallel handler was introduced.

Verified behavior:

- Station injects `since` and `exclude_invoice_ids`.
- Results use stable `invoice_id` cursors and deterministic oldest-to-newest ordering.
- Returned items expose provider `description` for workflow history matching.
- Complete results report `truncated: false`; capped results report `truncated: true`.
- Truncated execution cannot settle the schedule-owned watermark.
- Manual execution does not advance schedule-owned state.
- Station v0.66 expires timestamped seen cursors according to `seen_window_seconds` while preserving compatible legacy state.
- Whole-integer validation rejects booleans, floats, fractional strings, negative values, and other invalid inputs where applicable.

The manifest uses Semantic-Firewall-compatible schema type `number` for integer-valued fields; the handler still rejects non-whole values.

## AI and dunning boundaries

`stripe.billing.dunning_message_draft` reuses and modernizes the existing private legacy implementation as the 29th registered command.

Verified behavior:

- It is read-only, draft-only, and cannot send or authorize a financial action.
- Its prompt receives billing facts, not customer email, Stripe customer ID, or invoice ID.
- Output must be structured JSON containing validated `subject` and `body` fields.
- Malformed JSON, wrong shapes, invalid enums, unsafe references, and non-whole integer inputs fail closed.
- The other three AI commands retain minimized, structured, decision-support-only behavior.

RailCall-side credential resolution, destination, sandbox, Semantic Firewall, egress classification, and policy checks passed. A live Groq completion was not demonstrated, so this report does not claim one.

## Approval, vault, and idempotency

All 13 Stripe mutations are `write_requires_approval`, declare external side effects, and stop at Approval Airlock until the exact payload is approved. During full runtime verification, all 13 unapproved write scenarios produced `pending_approval`.

`_api_key()` resolves the Stripe credential from the local Station vault at execution time. The secret is used only for the Stripe `Authorization` header and is not included in command inputs, payload hashes, normal results, or receipts.

Stripe writes derive their `Idempotency-Key` from `airlock_payload_hash()`. The same approved payload retains the same effect identity; a changed payload changes the approval/hash binding. Missing idempotency support fails hard before an unprotected write. Idempotency is not automatic rollback.

Money inputs use strict integer cents. Boolean, float, fractional, zero where positive values are required, and negative inputs are rejected before provider execution.

## Runtime receipts

The completed module runtime produced 32 signed receipts:

- 8 `executed`;
- 10 honest failures;
- 13 `pending_approval`;
- 1 `blocked_by_policy`.

Payload-hash, integrity-hash, signature verification, and secret-leak checks passed. Actual normal Stripe `invoice_list` read compatibility was demonstrated. Provider success is claimed only where an actual provider receipt or effect supports it; fixture-only AI evidence is not labeled as live Groq success.

## Sandbox and capabilities

The final manifest requests only:

```json
{
  "network": ["api.stripe.com", "api.groq.com"],
  "subprocess": false,
  "filesystem_writes": []
}
```

Off-allowlist network access is denied. The handler receives no subprocess or filesystem-write capability. All 29 action IDs are unique, and Station v0.66 rejects action-ID collisions rather than silently overwriting a registration.

## Known limitations

- Live Groq completion remains unverified and must not be inferred from fixture or source evidence.
- The incremental/schedulable capability badge may not render consistently even though parser, injection, execution, and settlement behavior passed.
- Receipt-persistence failure in Station v0.66 surfaces `ok: false`, preserves staging, and reports the error, but the response may still contain `executed: true`; do not claim otherwise.
- Stripe usage aggregation is asynchronous, so `usage_summary_list` can lag after usage is recorded.

## Conclusion

Module v1.3.0 is final for Station v0.66: exactly 29 registered commands, 13 approval-controlled writes, incremental and schedulable `invoice_list`, privacy-hardened structured dunning drafting, strict validation, local-vault credential handling, approval-derived idempotency, required receipts, and signature tree v2. No module source migration was required for Station v0.66.
