# Testing report — dave/stripe-invoicing

Real command output from Stripe test mode, not a usage guide. Every result
below was run against the live module, not reconstructed. Timestamps are
UTC, 2026-07-28.

## Idempotency: retrying the same approved payload does not double-create

Two `customer_create` calls, identical input, both dispatched as separate
approvals.

Input (both calls):
```json
{"email": "testing-md-idem-2807@example.com", "name": "Testing MD Idempotency"}
```

Call 1:
```
result_status: executed
customer_id:   cus_UxyQV2HYaHRJYe
idempotency_key: idem_1978285362ae0cdfb7a53e1e
```

Call 2:
```
result_status: executed
customer_id:   cus_UxyQV2HYaHRJYe
idempotency_key: idem_1978285362ae0cdfb7a53e1e
```

Same `customer_id`, same idempotency key, both calls succeeded. Stripe's
account shows exactly one customer for this email. The airlock payload hash
is deterministic over identical inputs, so a retried approval keys to the
same Stripe Idempotency-Key and cannot create a duplicate.

## credit_note_create refuses a draft invoice

Draft invoice created fresh for this test: `in_1Ty4o0IiIXjQdConJKObLUaD`.

Call:
```json
{"invoice_id": "in_1Ty4o0IiIXjQdConJKObLUaD", "amount_cents": 2000}
```

Result:
```
result_status: failed_safely
note: execution failed: invoice in_1Ty4o0IiIXjQdConJKObLUaD is draft.
      a draft was never finalized, edit the draft directly instead.
```

The module's own precheck catches this before a Stripe call is made. Stripe
independently confirms the same rule with a real API call bypassing the
precheck: `POST /v1/credit_notes` against a draft invoice returns *"You
cannot create a credit note for a draft invoice."*

## promotion_code_create: a parameter shape that was wrong, then fixed

Stripe's `/v1/promotion_codes` endpoint does not accept a flat `coupon`
parameter, despite that being the natural first guess from the object's
name. The first implementation used it and failed every time.

Before the fix, direct call to Stripe:
```
POST /v1/promotion_codes
coupon=acALYzHV

-> "Received unknown parameter: coupon"
```

The correct shape is a nested object with a type discriminator:
`promotion[type]=coupon` plus `promotion[coupon]=<coupon_id>`.

After the fix, through the module:
```json
{"coupon_id": "acALYzHV", "code": "TESTINGMD01"}
```
```
result_status: executed
{
  "ok": true,
  "http_status": 200,
  "promotion_code_id": "promo_1Ty4oEIiIXjQdCon1cUDVPKW",
  "code": "TESTINGMD01",
  "coupon_id": "acALYzHV",
  "active": true
}
```

## Coverage

All twenty-five commands have been run against Stripe test mode at least
once, positive path and at least one failure path each, across the module's
build history. This file documents the three paths above in detail because
they are the ones that either moved money unsafely if wrong (idempotency),
or were found wrong by testing rather than by reading Stripe's docs
(credit note eligibility, promotion code parameters).
