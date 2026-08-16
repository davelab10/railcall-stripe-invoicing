# Stripe Invoicing for RailCall

Billing automation is useful only when an assistant cannot silently turn a read, retry, or AI suggestion into a financial effect. `dave/stripe-invoicing` gives RailCall Station a bounded Stripe surface with three deliberately different paths:

- **READ** — look up billing state and invoice history;
- **AI** — receive minimized facts and return structured decision support;
- **MONEY** — prepare Stripe mutations that stop at Approval Airlock until the exact payload is approved.

The practical path is **READ → PREFLIGHT → AI → MONEY**: reads supply provider context, preflight supplies bounded readiness evidence, AI remains advisory, and consequential writes stay behind Approval Airlock.

Current release: **v1.5.0**, with **34 commands**: **21 read** commands and **13 approval-controlled writes**.

Demo: https://youtu.be/GqG5-EciodI

## Install and first useful result

1. Install the module from the RailCall marketplace:

   ```sh
   railcall market install dave/stripe-invoicing
   ```

2. Reload Station and open the module in Studio. Confirm that `dave/stripe-invoicing` v1.5.0 is loaded.
3. In **Studio → Integrations → Stripe**, save a Stripe **TEST** secret in the Station vault. Do not paste a secret into command input.
4. Run the safe read `stripe.billing.customer_find` with a test-account email:

   ```json
   {
     "email": "billing-contact@example.test",
     "limit": 10
   }
   ```

   The command returns a normal read receipt; it does not mutate Stripe.
5. For bounded billing decision support, run `stripe.billing.account_preflight` with a Stripe customer ID:

   ```json
   {
     "customer_id": "cus_test_fixture"
   }
   ```

   The result is aggregate readiness evidence. It is not approval and does not authorize a charge. For a provider-backed renewal preview, run `stripe.billing.subscription_renewal_preview`:

   ```json
   {
     "customer_id": "cus_test_fixture"
   }
   ```

   With one eligible subscription, the handler resolves it and calls Stripe's explicit preview source. With none, multiple, truncated, or incomplete subscriptions, it returns bounded unknown/refusal semantics instead of guessing.
6. To request a real mutation, use the same Studio command surface with `stripe.billing.bill_client`:

   ```json
   {
     "email": "billing-contact@example.test",
     "description": "Monthly retainer",
     "amount_cents": 180000,
     "currency": "usd",
     "billing_run_id": "demo-2026-08-001"
   }
   ```

   The request must remain pending at Approval Airlock until an operator reviews and approves the exact payload. Use a Stripe TEST account for a first run.
7. Inspect the signed command receipt from the Studio run/receipt view. It records the decision and integrity evidence without putting the Stripe secret in the receipt.

The examples describe the input contract. They are not claims that a provider write was executed.

## Commands by job

Every command requires a Station receipt. `read` commands have no Stripe side effect; `write_requires_approval` commands declare an external effect and stop before provider execution.

### Customers and account state — read

| Command | What it does |
|---|---|
| `stripe.billing.customer_find` | Find customers by email. |
| `stripe.billing.customer_summary` | Combine customer profile, open invoices, subscriptions, and lifetime paid totals. |
| `stripe.billing.customer_balance_summary` | Summarize a customer's balance and billing exposure. |
| `stripe.billing.account_preflight` | Return bounded, privacy-minimized billing-readiness evidence for planning. |
| `stripe.billing.payment_method_list` | List saved payment-method brand and last four digits. |
| `stripe.billing.mandate_get` | Retrieve a mandate; mandate creation is not exposed. |

### Invoices, payments, and subscriptions

| Command | Mode | What it does |
|---|---|---|
| `stripe.billing.invoice_list` | read | List invoices manually or through Station-managed incremental history. |
| `stripe.billing.invoice_get` | read | Retrieve one invoice by ID. |
| `stripe.billing.subscription_list` | read | List subscriptions for a customer. |
| `stripe.billing.aging_report` | read | Bucket open invoices by days overdue. |
| `stripe.billing.invoice_preview` | read | Build an invoice preview without a Stripe effect. |
| `stripe.billing.subscription_renewal_preview` | read | Preview the next or recurring renewal through Stripe's provider preview endpoint. |
| `stripe.billing.payment_status_summary` | read | Summarize invoice payment status and overdue exposure. |
| `stripe.billing.customer_create` | write_requires_approval | Create a Stripe customer. |
| `stripe.billing.invoice_create` | write_requires_approval | Create a draft invoice with line items. |
| `stripe.billing.invoice_send` | write_requires_approval | Finalize and send an invoice. |
| `stripe.billing.invoice_void` | write_requires_approval | Void a finalized invoice. |
| `stripe.billing.subscription_cancel` | write_requires_approval | Cancel now or at period end. |
| `stripe.billing.bill_client` | write_requires_approval | Find or create a customer, create the invoice and line item, finalize it, and send it as one approved operation. |
| `stripe.billing.credit_note_create` | write_requires_approval | Issue a credit note against a finalized invoice. |
| `stripe.billing.refund_create` | write_requires_approval | Refund a charge. |

### Products, prices, discounts, and usage

| Command | Mode | What it does |
|---|---|---|
| `stripe.billing.coupon_list` | read | List coupons. |
| `stripe.billing.coupon_create` | write_requires_approval | Create a percent-off or amount-off coupon. |
| `stripe.billing.promotion_code_create` | write_requires_approval | Create a promotion code for an existing coupon. |
| `stripe.billing.product_create` | write_requires_approval | Create a product. |
| `stripe.billing.product_list` | read | List products. |
| `stripe.billing.price_create` | write_requires_approval | Create a one-time, recurring, or metered price. |
| `stripe.billing.price_list` | read | List prices. |
| `stripe.billing.usage_record_create` | write_requires_approval | Record usage for a billing meter. |
| `stripe.billing.usage_summary_list` | read | Read aggregated meter usage for a customer. |

### Decision support and AI — read-only

| Command | What it does |
|---|---|
| `stripe.billing.payment_risk_assess` | Assess payment risk from minimized account metrics. |
| `stripe.billing.collection_strategy_recommend` | Return a reviewable collections strategy. Non-JSON output gets one bounded governed retry, then fails closed if still invalid. |
| `stripe.billing.billing_anomaly_detect` | Detect anomalies in minimized billing-portfolio metrics. |
| `stripe.billing.dunning_message_draft` | Draft a privacy-minimized payment reminder. |

The 13 entries marked `write_requires_approval` are the complete approval-controlled write set. No AI command can approve, send, refund, void, cancel, or otherwise mutate Stripe state.

## v1.5.0 billing decision support

### `stripe.billing.account_preflight`

This is one bounded read for deciding whether a customer is ready to enter financial planning. It reads the customer, bounded invoice, subscription, and requested payment-method collections, then returns aggregate evidence:

- `billing_state`: `ready`, `attention_required`, `not_ready`, or `unknown`;
- invoice exposure, including open, overdue, partial, and uncollectible aggregates by currency;
- active subscription count and status counts;
- provider customer balance and its provider currency when supplied;
- attached payment-method counts and `has_usable_method`;
- per-source and overall `completeness`, `unknown_reasons`, and UTC `as_of`.

`has_usable_method` means only that Stripe returned an attached method of a requested type. It does not mean that a future authorization is guaranteed. If any bounded provider source is incomplete or truncated, the overall state remains `unknown` rather than becoming a false `ready` or `not_ready` result.

The projection does not return raw invoice rows, payment-method IDs or details, email, hosted URLs, or other unnecessary PII. It is decision-support evidence only: `approval_status` is not asserted by the module and `financial_authority` is false.

### `stripe.billing.subscription_renewal_preview`

This command uses Stripe's `POST /v1/invoices/create_preview` provider response. It never calculates a local amount from price, quantity, interval, discount, tax, or proration.

- With `subscription_id`, the handler uses that subscription explicitly (`selection: explicit_subscription`).
- With only `customer_id`, it performs a bounded subscription lookup. Exactly one eligible subscription (`active`, `trialing`, `past_due`, or `unpaid`) is used as the explicit preview source (`selection: resolved_single_subscription`).
- Zero eligible subscriptions returns `no_previewable_subscription`; multiple eligible subscriptions returns `ambiguous_subscription`; neither is selected arbitrarily.
- A truncated, incomplete, malformed, or refused subscription lookup fails closed with bounded unknown semantics.
- `amount_due_cents`, `currency`, `period_start`, `period_end`, `next_payment_due`, discount/tax indicators, proration state, and line completeness are reported only from provider evidence. Missing or incomplete provider fields remain unknown.

The resolved subscription ID is used internally and is not returned as a public output field. Renewal previews can change before a later invoice is created; a preview is read-only decision support, not a financial effect.

### Live provider finding

During Stripe TEST verification, the customer-only form was rejected because `create_preview` requires an explicit preview source. The implementation now resolves a single eligible subscription deterministically before calling the provider endpoint, with no deprecated-endpoint or local-estimate fallback.

## Incremental invoice history

The existing `stripe.billing.invoice_list` supports both an ordinary manual read and Station-managed incremental execution; no parallel command or handler was added.

- Station injects `since` and `exclude_invoice_ids` for an incremental run.
- Results use stable provider invoice IDs and deterministic oldest-to-newest ordering.
- Each item exposes the provider `description`, which lets the Retainer Billing workflow distinguish `Retainer billing for <period>` from unrelated invoices.
- Complete results report `truncated: false`; capped partial results report `truncated: true`.
- A truncated result cannot settle the schedule-owned watermark.
- Manual runs do not advance schedule-owned state.
- Station owns the cursor, seen-window behavior, and watermark; the module does not write a local cursor file.

The schedulable contract uses `skip` concurrency, a 15-minute minimum interval, and a five-minute maximum runtime.

## AI boundary and fail-closed behavior

The four AI commands are read-only, structured, minimized, and governed. A dunning draft receives billing facts—not customer email, Stripe customer ID, or invoice ID—and returns validated JSON with `subject` and `body`. It cannot send the draft or authorize a financial action.

Malformed or non-JSON output, unsafe references, wrong shapes, invalid enums, and invalid whole-integer inputs are rejected. `collection_strategy_recommend` has one bounded retry for non-JSON output; a second invalid response still fails closed. Final governed AI verification covered all five required scenarios.

## Financial boundaries

### Approval and receipts

Every Stripe mutation is `write_requires_approval`, declares an external side effect, and stops at Approval Airlock until the exact payload is approved. Station owns approval state, signatures, receipt persistence, and egress policy.

Successful commands and governed provider egress produce signed receipts that can be inspected from the Studio run/receipt view. The receipt is evidence of the recorded action and integrity hashes; it is not a substitute for provider confirmation when the provider outcome is unknown.

### Idempotency

Writes derive Stripe's `Idempotency-Key` from Station's approved `airlock_payload_hash()`. Retrying the same approved payload retains the same effect identity; changing the payload changes its approval/hash binding. Missing idempotency support fails before an unprotected write. Idempotency protects safe retries; it is not automatic rollback.

### Vault and sandbox

Configure Stripe through **Studio → Integrations → Stripe**. The handler resolves `vault_get("stripe")` at execution time and supports the existing `STRIPE_SECRET_KEY`, `api_key`, `secret_key`, and `token` fields. The secret is used only in the Stripe `Authorization` header and is not added to command inputs, payload hashes, normal results, or receipts.

The module requests only the capabilities needed for its providers:

```json
{
  "network": ["api.stripe.com", "api.groq.com"],
  "subprocess": false,
  "filesystem_writes": []
}
```

## Testing and proof

The current v1.5.0 regression result is **50/50 PASS**. The proof set covers command registration and schema checks, existing read/write safeguards, incremental invoice history, dunning and advisory fail-closed behavior, Stripe TEST verification for `account_preflight`, the corrected provider-backed renewal preview, and module signature verification. Stripe TEST evidence is not production-money verification.

## Known limitations

- Provider success should be claimed only when an actual provider receipt or effect supports it. Stripe verification was performed against a TEST account, not a production account.
- AI commands require a configured governed provider and fail closed when that path is unavailable.
- An attached payment method does not guarantee a successful future authorization.
- Bounded preflight returns `unknown` when an invoice, subscription, or payment-method source is incomplete or truncated.
- A renewal preview can change before the actual invoice is created.
- Multiple eligible subscriptions are reported as ambiguous; the handler does not choose one arbitrarily.
- Provider/API refusal is not replaced with a local financial estimate.
- Customer balance currency is left unknown when Stripe does not provide an authoritative currency.
- Stripe usage aggregation is asynchronous; `usage_summary_list` can lag after usage is recorded.
- `subscription_renewal_preview` reports provider preview evidence only; it does not create or send an invoice.
- `credit_note_create` is for open/finalized invoice cases supported by the provider; paid invoices should use `refund_create`.
- `invoice_void` applies to open/finalized invoices; drafts should be edited and paid invoices refunded.
- `price_create` requires an interval when `meter_id` is supplied.
- Subscription support is cancel-only, and each invoice uses one currency.

For regression details and evidence boundaries, see [TESTING.md](TESTING.md).
