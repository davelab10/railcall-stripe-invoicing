# Stripe Invoicing for RailCall

`dave/stripe-invoicing` v1.3.0 provides governed Stripe accounts-receivable operations for RailCall Station v0.66. It is designed for operators who need automation without allowing an agent or LLM to move money independently.

Demo video: https://youtu.be/C6INOasnOsM

## Command surface

The module registers exactly 29 commands under `stripe.billing.*`:

- 16 `read` commands with no external side effects;
- 13 `write_requires_approval` commands that stop at Approval Airlock;
- all 29 require Station receipts.

Read commands include Stripe lookup/reporting operations and four governed AI decision-support commands:

- `stripe.billing.payment_risk_assess`
- `stripe.billing.collection_strategy_recommend`
- `stripe.billing.billing_anomaly_detect`
- `stripe.billing.dunning_message_draft`

Approval-controlled writes cover customer, invoice, subscription, credit-note, refund, coupon, promotion-code, product, price, usage, and composite billing operations. `stripe.billing.bill_client` remains the composite operation: find or create a customer, create the invoice and line item, finalize it, and send it behind one exact-payload approval.

## Incremental invoice listing

The existing `stripe.billing.invoice_list` supports ordinary manual reads and Station-managed incremental execution; no parallel incremental handler was added.

- Station injects `since` and `exclude_invoice_ids`.
- Results use stable provider `invoice_id` cursors and deterministic oldest-to-newest ordering.
- Each invoice exposes provider `description` for deterministic workflow history matching.
- Complete results report `truncated: false`; capped partial results report `truncated: true`.
- Truncated runs cannot settle the schedule-owned watermark.
- Manual runs do not advance schedule-owned state.
- Station v0.66 expires timestamped seen cursors according to the declared window while preserving compatible legacy state.

The schedulable contract uses concurrency `skip`, a 15-minute minimum interval, and a 5-minute maximum runtime. It is consumed by workflows such as `dave/retainer-billing-run` rather than by creating another command.

## AI decision support

All four AI commands are read-only, structured, minimized, and fail closed. They cannot create, send, void, refund, cancel, approve, or otherwise modify Stripe state.

`stripe.billing.dunning_message_draft` reuses and modernizes the existing private legacy implementation. It receives billing facts—not customer email, Stripe customer ID, or invoice ID—and returns a validated JSON draft with `subject` and `body`. It cannot send the draft or authorize a financial action.

Invalid JSON, wrong output shapes, unsafe references, invalid enums, non-whole integer inputs, and out-of-range values return `invalid_ai_response` or an input-validation error. Live Groq completion has not been demonstrated and is not claimed here or in the demo video.

## Financial guardrails

### Approval

Every Stripe mutation is `write_requires_approval`, declares external side effects, and stops before provider execution until the exact payload is approved. Read and AI commands cannot mutate Stripe.

### Idempotency

Writes derive the Stripe `Idempotency-Key` from Station's approved `airlock_payload_hash()`. Retrying the same approved payload retains the same effect identity; changing the payload changes its approval/hash binding. Missing idempotency support fails hard before an unprotected write. Idempotency is safe-retry protection, not automatic rollback.

### Strict money validation

Money inputs use integer cents. Booleans, floats, fractional strings, zero where a positive amount is required, and negative values are rejected before provider execution. The manifest uses Semantic-Firewall-compatible type `number` where needed, while the handler still enforces whole integers.

### Receipts

Every command requires a Station receipt. Approval state, result state, payload/integrity hashes, signatures, and governed egress remain Station responsibilities. Provider success should be claimed only when supported by an actual provider receipt or effect.

## Credentials and sandbox

Configure Stripe through Studio → Integrations → Stripe. The handler resolves `vault_get("stripe")` at execution time and supports the existing `STRIPE_SECRET_KEY`, `api_key`, `secret_key`, and `token` fields. The key is used only in the Stripe `Authorization` header; it is not added to command inputs, payload hashes, normal results, or receipts.

The final least-privilege capabilities are:

```json
{
  "network": ["api.stripe.com", "api.groq.com"],
  "subprocess": false,
  "filesystem_writes": []
}
```

The AI commands use Station's managed Groq credential and governed LLM path. The module does not request subprocess execution or filesystem writes.

## Install

```sh
railcall market install dave/stripe-invoicing
```

After Station reloads, the module should report v1.3.0 with exactly 29 commands and no missing handlers.

## Example

`stripe.billing.bill_client` accepts one client billing request and presents one approval:

```json
{
  "email": "billing-contact@example.test",
  "description": "Monthly retainer",
  "amount_cents": 180000
}
```

This documents the input contract only; it is not a claim of provider execution.

## Known limitations

- Live Groq completion remains unverified.
- Studio may not render incremental/schedulable capability badges consistently even though parsing, injection, execution, and settlement passed.
- Stripe usage aggregation is asynchronous, so `usage_summary_list` can lag after usage is recorded.
- `credit_note_create` is limited to open invoices; paid invoices should use `refund_create`.
- `invoice_void` applies to open invoices; drafts should be edited and paid invoices refunded.
- `price_create` requires an interval when `meter_id` is supplied.
- Subscription support is cancel-only, and invoices use one currency per invoice.
