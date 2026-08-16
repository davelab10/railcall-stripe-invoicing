# Testing report — `dave/stripe-invoicing` v1.5.0

Demo video: https://youtu.be/bFjjwo7R5JU

This report separates automated regression, Stripe TEST verification, governed AI verification, and receipt/policy evidence. A fixture or refusal is never presented as provider success.

## Final status

| Check | Result |
|---|---|
| Module identity | `dave/stripe-invoicing` v1.5.0 |
| Command registration | 34/34 resolved |
| Classification | 21 `read`; 13 `write_requires_approval` |
| Module regression | 50/50 PASS |
| Stripe provider verification | PASS against Stripe TEST API |
| Governed AI verification | PASS, 5/5 final scenarios |
| Incremental/schedulable contract | PASS |
| Signed command receipts | PASS |
| Governed egress receipts | PASS |
| Action-ID uniqueness | PASS |
| Secret-pattern checks | PASS; no secrets in audited outputs |

## Automated regression

The final module suite covers registration and command resolution, manifest/schema compatibility, strict input validation, receipt requirements, capability boundaries, incremental parsing, scheduling injection, dunning privacy, AI fail-closed behavior, approval classification, vault/idempotency handling, and action-ID uniqueness.

The final result was **50/50 PASS**. All 34 commands resolve, and the 13 mutation commands remain `write_requires_approval` with `receipt_required: true`.

The v1.5.0 additions are `stripe.billing.account_preflight` and `stripe.billing.subscription_renewal_preview`. Both are read-only and provider-backed; neither can approve, execute, or authorize a Stripe mutation.

## Stripe TEST API verification

The provider-facing checks used a Stripe TEST account. Verified paths included:

- ordinary customer/invoice reads and the incremental-compatible `invoice_list` read;
- `bill_client` through its approval-controlled composite path;
- subscription cancellation;
- refund and credit-note handling;
- usage meter/event recording followed by usage summary lookup;
- mandate retrieval;
- relevant cross-command customer → invoice → payment-state flows.
- `stripe.billing.account_preflight` against Stripe TEST customer, invoice, subscription, balance, and payment-method reads;
- `stripe.billing.subscription_renewal_preview` through `POST /v1/invoices/create_preview`, with provider-returned amount, currency, period, and supported indicators only.

Results claimed as provider behavior are limited to receipts/effects actually returned by that TEST account. No production-money execution is implied.

The first useful read is `stripe.billing.customer_find`; `stripe.billing.invoice_list` was also exercised for ordinary and incremental-compatible reads. Financial writes stop at Approval Airlock until the exact payload is approved.

The renewal preview never falls back to the deprecated Upcoming Invoice API or local arithmetic. With an explicit subscription it previews that subscription; customer-only selection resolves exactly one eligible subscription and fails closed for zero, multiple, malformed, incomplete, or truncated candidates.

## Incremental `invoice_list`

The existing command supports manual reads and Station-managed incremental execution. No second incremental handler was introduced.

Verified behavior:

- Station injects `since` and `exclude_invoice_ids` without the workflow hardcoding either field.
- Results use stable provider invoice IDs and deterministic ordering.
- Returned items expose provider `description` for deterministic retainer-history matching.
- Complete results report `truncated: false`; capped results report `truncated: true`.
- Truncated execution cannot settle the schedule-owned watermark.
- Manual execution does not advance schedule-owned state.
- Seen-window and watermark state remain Station-owned.
- Whole-integer validation rejects booleans, floats, fractional strings, negative values, and other invalid values where the command contract requires a whole integer.

## AI and dunning verification

`stripe.billing.dunning_message_draft` reuses and privacy-hardens the legacy private implementation. The final verification covered the five required AI scenarios, including the bounded non-JSON retry path.

Verified behavior:

- The command is read-only and draft-only.
- The prompt contains billing facts, not customer email, Stripe customer ID, or invoice ID.
- The result must be structured JSON with validated `subject` and `body` fields.
- Malformed/non-JSON output, wrong shapes, unsafe references, invalid enums, and invalid whole-integer inputs fail closed.
- `stripe.billing.collection_strategy_recommend` retries a non-JSON response once through the governed path; a second invalid response is rejected.
- AI output cannot authorize or execute a Stripe mutation.

Governed live AI provider verification is **5/5 PASS** using minimized/synthetic billing facts. This does not give AI financial authority.

## Approval, vault, idempotency, and receipts

All 13 Stripe mutations declare an external effect and stop at Approval Airlock until the payload is approved. The final verification exercised the pending-approval boundary and the approved-payload identity used for safe retries.

- `_api_key()` resolves the Stripe credential from the local Station vault at execution time.
- The secret is used only in the Stripe `Authorization` header and is not included in command inputs, payload hashes, normal results, or receipts.
- Stripe writes derive `Idempotency-Key` from `airlock_payload_hash()`.
- Repeating the same approved payload retains the same effect identity; changing the payload changes the approval/hash binding.
- Missing idempotency support fails before an unprotected write.
- Idempotency is not automatic rollback.
- Signed command receipts and governed-egress receipts both verified.

## Capabilities and action surface

The final capability boundary is:

```json
{
  "network": ["api.stripe.com", "api.groq.com"],
  "subprocess": false,
  "filesystem_writes": []
}
```

All 34 action IDs are unique. The module does not request subprocess or filesystem-write access. Receipt and egress checks did not find secret-pattern leakage in audited outputs.

## Environment boundaries and known limitations

- Stripe provider evidence is from a TEST account; production account behavior is not implied.
- AI verification requires the governed provider path and its configured credential; unavailable or malformed AI responses fail closed.
- Stripe usage aggregation is asynchronous, so `usage_summary_list` can lag after a usage record is written.
- Provider success is claimed only when an actual receipt or effect supports it.
- `account_preflight` and `subscription_renewal_preview` are read-only provider evidence; they do not create invoices, charge customers, or grant approval authority.

## Conclusion

Module v1.5.0 is the verified contest state: 34 resolved commands, 21 reads, 13 approval-controlled writes, incremental/schedulable invoice history, account billing preflight, provider-backed subscription renewal preview, privacy-minimized structured AI, bounded retry behavior, vault-local credentials, approval-derived idempotency, signed evidence, and no production-money execution in the verification set.
