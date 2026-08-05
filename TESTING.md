# Testing report — dave/stripe-invoicing v1.2.5

Target runtime: RailCall Station v0.55.

This report records the provider-independent Step 3 checks that were actually completed and separates them from provider calls that Station could not complete.

Status legend:

- ✅ **Verified** — directly verified without requiring a successful live Stripe or Groq response.
- ⚠ **Blocked by Station** — execution reached the official provider path but Station v0.55 blocked the allowed hostname after DNS resolution.
- 🧪 **Fixture Only** — the handler and validation path were exercised with controlled fixtures or a substituted helper/provider result; this is not a live-provider pass.

Live Stripe and Groq execution are not marked as passed anywhere in this report.

## Static and loader verification

| Check | Status | Evidence |
|---|---|---|
| Manifest parses as JSON | ✅ Verified | `module/module.json` loaded successfully. |
| Module identity and version | ✅ Verified | `dave/stripe-invoicing`, v1.2.5. |
| Command count | ✅ Verified | Exactly 28 manifest commands. |
| Classification | ✅ Verified | 15 `read`; 13 `write_requires_approval`. |
| Handler registration | ✅ Verified | All 28 public manifest command IDs resolve to top-level handlers; no missing public handler. |
| Python compilation | ✅ Verified | `handler.py` compiled during Step 3 validation. |
| Module load | ✅ Verified | Station loaded v1.2.5 with 28 commands and no module rejection. |
| Signature | ✅ Verified | Manifest v2/tree signature verification succeeded before Station accepted the module. |
| Receipt declaration | ✅ Verified | Every manifest command declares `receipt_required: true`. |
| Sandbox declaration | ✅ Verified | Network allowlist contains only `api.stripe.com` and `api.groq.com`; subprocess is false; filesystem writes are empty. |

## Command matrix

The fixture status below means command dispatch, handler binding, input validation, and controlled success/failure handling were exercised. It does not mean Stripe or Groq returned a live success response during Step 3.

| Command | Mode | Step 3 result | Live provider |
|---|---|---|---|
| `stripe.billing.customer_find` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.invoice_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.invoice_get` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.subscription_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.customer_summary` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.aging_report` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.payment_method_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.coupon_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.product_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.price_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.usage_summary_list` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.mandate_get` | read | 🧪 Fixture Only | ⚠ Blocked by Station |
| `stripe.billing.customer_create` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.invoice_create` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.invoice_send` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.invoice_void` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.subscription_cancel` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.bill_client` | write_requires_approval | 🧪 Composite fixture only; approval and idempotency paths verified | ⚠ Blocked by Station |
| `stripe.billing.credit_note_create` | write_requires_approval | 🧪 Fixture Only; status precheck verified | ⚠ Blocked by Station |
| `stripe.billing.refund_create` | write_requires_approval | 🧪 Fixture Only; input validation verified | ⚠ Blocked by Station |
| `stripe.billing.coupon_create` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.promotion_code_create` | write_requires_approval | 🧪 Fixture Only; request shape verified | ⚠ Blocked by Station |
| `stripe.billing.product_create` | write_requires_approval | 🧪 Fixture Only; approval policy verified | ⚠ Blocked by Station |
| `stripe.billing.price_create` | write_requires_approval | 🧪 Fixture Only; input combinations verified | ⚠ Blocked by Station |
| `stripe.billing.usage_record_create` | write_requires_approval | 🧪 Fixture Only; approval and idempotency paths verified | ⚠ Blocked by Station |
| `stripe.billing.payment_risk_assess` | read | 🧪 Structured-response and fail-closed fixtures verified | ⚠ Blocked by Station |
| `stripe.billing.collection_strategy_recommend` | read | 🧪 Structured-response and fail-closed fixtures verified | ⚠ Blocked by Station |
| `stripe.billing.billing_anomaly_detect` | read | 🧪 Structured-response, PII rejection, and reference fixtures verified | ⚠ Blocked by Station |

## Governance and approval

| Check | Status | Result |
|---|---|---|
| All Stripe mutations require approval | ✅ Verified | All 13 write commands are `write_requires_approval`, `side_effects: external`, and `preview: true`. |
| Read and AI commands avoid mutation | ✅ Verified | All 15 are `read` with `side_effects: none`. |
| Approval boundary precedes provider execution | ✅ Verified | Write execution entered Station's approval path before the provider attempt. |
| Receipt requirement | ✅ Verified | All 28 command definitions require receipts. |
| Live signed Stripe result | ⚠ Blocked by Station | No current live Stripe success is claimed. |
| Live AI egress result | ⚠ Blocked by Station | No current live Groq success or completed provider response is claimed. |

## Idempotency and spend protection

| Check | Status | Result |
|---|---|---|
| Approved-payload hash becomes Stripe idempotency key | 🧪 Fixture Only | Controlled helper output was propagated to the `Idempotency-Key` header for write requests. |
| Missing `airlock_payload_hash` helper | ✅ Verified | Handler fails hard before an unprotected write can be sent. |
| Composite `bill_client` scopes individual sub-effects | 🧪 Fixture Only | Fixture execution produced distinct indexed scopes for its internal writes. |
| Zero, negative, boolean, and float money values | ✅ Verified | Strict integer-cent validation rejects unsafe values before provider execution. |
| Aggregate spend cap | Not a module claim | Native cumulative spend caps belong to the workflow/Station execution policy. At module level, approval, integer-cent validation, and idempotency are the spend protections tested here. |

## Fail-closed validation

The following provider-independent negative paths were verified:

- Missing or malformed required strings are rejected.
- Float cents are rejected rather than truncated.
- Zero and negative amounts are rejected where the command requires a positive amount.
- Invalid invoice transitions fail before mutation, including unsupported credit-note and void states.
- Invalid refund fields and unsupported combinations are rejected.
- `at_period_end` accepts valid booleans and rejects invalid values.
- Credential absence and malformed key prefixes fail without exposing a secret.
- Missing idempotency support stops writes rather than silently omitting the header.

## AI validation and minimization

All three AI commands call Station's governed LLM entry point; the handler does not send a direct HTTP request to Groq.

| Check | Status | Result |
|---|---|---|
| Commands are `read` / `side_effects: none` | ✅ Verified | Manifest classification matches all three handlers. |
| `decision_support_only` | 🧪 Fixture Only | Valid fixture results add `decision_support_only: true` and a human-review note. |
| Structured JSON required | 🧪 Fixture Only | Non-JSON and non-object replies return `invalid_ai_response`. |
| Required fields and enums | 🧪 Fixture Only | Missing fields and invalid enum values are rejected. |
| Integer ranges | 🧪 Fixture Only | Risk score, wait days, counts, and money metrics reject booleans, floats, and out-of-range integers. |
| Array bounds and item types | 🧪 Fixture Only | Drivers, anomalies, and review order enforce length and type constraints. |
| Opaque anomaly references | 🧪 Fixture Only | Unknown, repeated, email-like, `cus_`, and `in_` references are rejected. |
| PII minimization | ✅ Verified | Prompts are assembled only from minimized aggregates or opaque references. |
| Live Groq response and egress receipt | ⚠ Blocked by Station | Provider execution stops at the Station v0.55 DNS sandbox issue; not marked passed. |

## Sandbox verification

| Check | Status | Result |
|---|---|---|
| Manifest capability shape | ✅ Verified | `requires.network`, `requires.subprocess`, and `requires.filesystem_writes` match Station v0.55's manifest shape. |
| Off-allowlist network destination | ✅ Verified | Denied by sandbox policy. |
| Subprocess execution | ✅ Verified | Not granted by the manifest. |
| Filesystem writes | ✅ Verified | No writable filesystem capability is requested. |
| Allowed Stripe and Groq hostname execution | ⚠ Blocked by Station | The hostname is allowed, but Station resolves it to an IP and then rejects the IP because it is not a hostname entry in the allowlist. |

## Signature verification

Station accepted the v1.2.5 module only after verifying its manifest v2/tree signature and signed file set. This provider-independent verification passed. Provider execution failure does not invalidate the module signature.

## Known Station blocker

Live provider execution currently stops in RailCall Station v0.55 after DNS resolution:

1. The manifest permits `api.stripe.com` and `api.groq.com`.
2. The sandbox resolves the hostname to an IP address.
3. The runtime compares the resolved IP with the hostname allowlist.
4. The IP is rejected even though its originating hostname is allowed.

This is recorded as **⚠ Blocked by Station**, not as a module failure and not as a Stripe or Groq pass. No fixture was substituted to claim a live end-to-end provider success.

## Step 3 conclusion

- Manifest, registration, classification, approval boundaries, strict validation, idempotency failure behavior, signature, and sandbox declarations are ✅ verified.
- All 28 commands have provider-independent fixture coverage, clearly marked 🧪 Fixture Only.
- Current live Stripe and Groq provider completion remains ⚠ blocked by Station v0.55.
- No secret, credential, approval token, or live provider identifier is included in this report.
