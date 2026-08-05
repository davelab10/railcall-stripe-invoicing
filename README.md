# Stripe Invoicing for RailCall

`dave/stripe-invoicing` v1.2.5 provides governed Stripe accounts-receivable operations for RailCall Station. It is designed for operators who need automation without allowing an agent or an LLM to move money independently.

Demo video: https://youtu.be/7wtXK724Kig

## Command surface

The module registers exactly 28 commands under `stripe.billing.*`:

- 15 `read` commands with no external side effects.
- 13 `write_requires_approval` commands that stop at the Station airlock.
- All 28 declare `receipt_required: true`.

### Read commands

- `stripe.billing.customer_find`
- `stripe.billing.invoice_list`
- `stripe.billing.invoice_get`
- `stripe.billing.subscription_list`
- `stripe.billing.customer_summary`
- `stripe.billing.aging_report`
- `stripe.billing.payment_method_list`
- `stripe.billing.coupon_list`
- `stripe.billing.product_list`
- `stripe.billing.price_list`
- `stripe.billing.usage_summary_list`
- `stripe.billing.mandate_get`
- `stripe.billing.payment_risk_assess`
- `stripe.billing.collection_strategy_recommend`
- `stripe.billing.billing_anomaly_detect`

### Approval-controlled write commands

- `stripe.billing.customer_create`
- `stripe.billing.invoice_create`
- `stripe.billing.invoice_send`
- `stripe.billing.invoice_void`
- `stripe.billing.subscription_cancel`
- `stripe.billing.bill_client`
- `stripe.billing.credit_note_create`
- `stripe.billing.refund_create`
- `stripe.billing.coupon_create`
- `stripe.billing.promotion_code_create`
- `stripe.billing.product_create`
- `stripe.billing.price_create`
- `stripe.billing.usage_record_create`

`stripe.billing.bill_client` remains the composite billing operation: find or create a customer, create the invoice and line item, finalize it, and send it behind one human approval.

## AI decision support

The three AI commands are read-only decision-support tools:

- `stripe.billing.payment_risk_assess` evaluates minimized aggregate payment metrics and returns a structured risk level, score, drivers, and review recommendation.
- `stripe.billing.collection_strategy_recommend` returns a reviewable next-step strategy from minimized delinquency facts. It does not draft or send messages.
- `stripe.billing.billing_anomaly_detect` reviews an anonymized billing portfolio using opaque record references.

All three use Station's governed LLM entry point with Groq provider routing. They return `decision_support_only: true`, require structured JSON, and reject invalid schemas as `invalid_ai_response`. Enum values, required fields, integer ranges, list sizes, and opaque record references are validated fail-closed.

AI commands never create, send, void, refund, cancel, or otherwise modify Stripe state. They never approve a charge and are not a financial gate. Each call is attributed to this module and uses Station's egress-governance path.

Data sent to the LLM is minimized:

- Payment risk uses aggregate counts and amounts only.
- Collection strategy uses delinquency facts without customer, invoice, email, or account identifiers.
- Anomaly detection rejects email addresses and Stripe-style customer or invoice IDs in `record_ref`.

## Financial guardrails

### Human approval

Every write is classified as `write_requires_approval`, has external side effects, and must pass through the Station airlock. Read and AI commands cannot mutate Stripe.

### Idempotency

Stripe writes use an `Idempotency-Key` derived from Station's approved airlock payload hash. Retrying the same approved payload therefore reuses the same effect identity. If the `airlock_payload_hash` helper is unavailable, execution fails hard instead of sending an unprotected write.

### Strict money validation

Money inputs use integer cents. Booleans, floats, fractional strings, zero where a positive amount is required, and negative values are rejected before provider execution. The handler never silently truncates a float.

### Receipts

Every command requires a Station receipt. Approval state, result state, and governed LLM egress are recorded by Station; receipt signing and verification remain platform responsibilities.

## Sandbox capabilities

The v1.2.5 manifest declares least-privilege capabilities:

```json
{
  "network": ["api.stripe.com", "api.groq.com"],
  "subprocess": false,
  "filesystem_writes": []
}
```

`allowed_destinations` contains only the governed Groq provider destination. The module does not request subprocess execution or filesystem writes.

## Credentials

Open Studio → Integrations → Stripe → Configure and save a Stripe secret key. Station v0.48+ can resolve the default named credential, while the handler remains backward compatible with existing `STRIPE_SECRET_KEY` entries and the aliases `api_key`, `secret_key`, and `token`. No credential migration is required.

The key is read from the local vault at call time and sent only in the Stripe `Authorization` header. It is not added to command inputs, payload hashes, or module results. Stripe test keys and restricted secret keys are accepted; publishable keys are rejected.

The three AI commands require the Station-managed Groq credential used by `station.llm`.

## Install

```sh
railcall market install dave/stripe-invoicing
```

After restarting or reloading Station, the module should report v1.2.5 with 28 commands and no missing handlers.

## Example

`stripe.billing.bill_client` accepts one client billing request and presents one approval:

```json
{
  "email": "billing-contact@example.test",
  "description": "Monthly retainer",
  "amount_cents": 180000
}
```

This example documents the input contract only. It is not a claim that the current Station v0.55 provider call completed; see the limitation below.

## Known limitations

- **Station v0.55 sandbox DNS bug:** live Stripe and Groq provider execution is currently blocked when an allowed hostname resolves to an IP address and the runtime checks that IP against the hostname allowlist. The module and manifest validate correctly; this is an official Station runtime limitation already reported to the core team.
- Amounts are integer cents. For example, use `25000`, not `250.00`.
- `credit_note_create` is limited to open invoices. Paid invoices should use `refund_create`.
- `invoice_void` is for open invoices; drafts should be edited and paid invoices refunded.
- `price_create` requires an interval when `meter_id` is supplied.
- Usage aggregation is asynchronous, so `usage_summary_list` may remain empty for roughly 20–30 seconds after recording usage.
- `mandate_get` is read-only because mandates are created as a side effect of supported payment setup flows.
- Invoices use one currency, defaulting to USD.
- Subscription support is cancel-only.
- `payment_method_list` defaults to `card`; pass another explicit type when needed.
- Studio's manifest schema does not express a boolean type for `at_period_end`; the handler enforces valid boolean input.
