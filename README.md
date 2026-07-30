# Stripe Invoicing for RailCall

Bill and collect, without handing an agent your Stripe account.

Demo video: https://youtu.be/Uk6ikZmFR1g

## What it does

Twenty-five commands under `stripe.billing.*`, covering accounts receivable
end to end. Twelve reads run without approval, covering customers, invoices,
subscriptions, payment methods, coupons, products, prices, usage, and
mandates. `aging_report` buckets every open invoice by days overdue.
`customer_summary` rolls up profile, invoices, subscriptions, and lifetime
paid into one call instead of three.

Thirteen writes go through the airlock: create a customer, draft and send an
invoice, void a mistake, cancel a subscription, issue a credit note or a
refund, manage coupons, products, and prices, record metered usage, and
`bill_client`, which chains find-or-create, draft, finalize, and send behind
a single approval. `price_create` covers one-time, recurring, and
usage-based pricing via Stripe's Billing Meters API; usage aggregation is
async, so a summary read right after recording can come back empty for up
to about thirty seconds.

Every write that commits money carries a Stripe `Idempotency-Key` derived
from the approved payload hash. One approval is at most one effect. A missing
idempotency helper is a hard error, not a silent fallback that would quietly
drop double-charge protection on a retry.

## Who it is for

The solo consultant or small agency who runs billing from an assistant. "Bill
Ada 1800 dollars for the August retainer" should be one sentence and one
approval, not a model improvising Stripe calls on its own.

## Install

```
railcall market install dave/stripe-invoicing
```

Restart Studio; the startup log lists all twenty-five commands.

## Credentials

One Stripe secret key, held locally, sent only as an `Authorization` header.
Never in a request body, payload hash, or receipt.

**Save it under the legacy `STRIPE_SECRET_KEY` field** in Studio →
Integrations → the stripe card. The newer "Add credential" button writes to a
store handlers cannot read.

`sk_test_` keys work.

## Worked example

`stripe.billing.bill_client`, one command, one approval:

```json
{"email": "bill-client-test-2707@example.com",
 "description": "August retainer", "amount_cents": 180000}
```

Actual output:

```json
{"ok": true, "http_status": 200, "customer_id": "cus_Uxiyhn2Wgeyiaf",
 "customer_created": true, "invoice_id": "in_1TxnWRIiIXjQdCon4iHziWlw",
 "status_at_stripe": "open", "amount_due": "1800.00 USD"}
```

Four Stripe calls, one signed receipt: the customer did not exist, so it was
created, invoiced, and sent.

## Known limitations

- **Amounts are integer cents.** `250.00` is rejected; pass `25000`.
- **`credit_note_create` only works on open invoices**; a paid one needs
  `refund_create` instead, since Stripe requires a matching refund amount.
- **Void is only for open invoices.** Drafts are deleted, paid invoices
  refunded.
- **`mandate_get` is read-only.** Mandates cannot be created through the
  Stripe API, only as a side effect of a SEPA or ACH setup flow.
- **`price_create`'s `meter_id` requires `interval` too**, since a metered
  price is still billed on a schedule.
- **One currency per invoice**, defaulting to USD.
- **Subscriptions are cancel-only.** No creating or repricing plans.
- **`payment_method_list` defaults to `type: card`.** Pass a different type
  for anything else; Stripe returns nothing for a type not explicitly asked.
- **`at_period_end` declares no type**, since Studio's validator has no
  boolean support; the handler enforces true/false itself.
