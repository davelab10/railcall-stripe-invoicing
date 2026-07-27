# Stripe Invoicing for RailCall

Bill a client and collect, without handing an agent your Stripe account.

## What it does

Eighteen commands under `stripe.billing.*`, covering the full accounts
receivable cycle. Eight reads run without approval: find a customer, list or
retrieve invoices, list subscriptions, list payment methods, list coupons,
plus two that save real digging. `aging_report` buckets every open invoice by
days overdue. `customer_summary` rolls up profile, open invoices, active
subscriptions, and lifetime paid into one call instead of three.

Ten writes go through the airlock: create a customer, draft and send an
invoice, void a mistake, cancel a subscription, issue a credit note or a
refund, create coupons and promotion codes, and `bill_client`, which chains
find-or-create-customer, draft, finalize, and send behind a single approval.
The daily case is one command instead of four.

Every write that commits money carries a Stripe `Idempotency-Key` derived
from the approved payload hash. One approval is at most one effect. A missing
idempotency helper is a hard error, not a silent fallback that would quietly
drop double-charge protection on a retry.

## Who it is for

The solo consultant or small agency who runs billing from an assistant. "Bill
Ada 1800 dollars for the August retainer" should be one sentence and one
approval, not a model improvising four separate Stripe calls on its own
judgment.

## Install

```
railcall market install dave/stripe-invoicing
```

Restart Studio. The startup log lists the eighteen `stripe.billing.*`
commands.

## Credentials

One Stripe secret key, held locally and sent only as an `Authorization`
header. It never enters a request body, a payload hash, or a receipt.

**Save it under the legacy `STRIPE_SECRET_KEY` field** in Studio →
Integrations → the stripe card. The newer "Add credential" button writes to a
different store that handlers cannot read.

`sk_test_` keys work end to end.

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

Four Stripe calls behind one signed receipt: the customer did not exist, so
it was created, then invoiced, then sent.

## Known limitations

- **Amounts are integer cents.** `250.00` is rejected; pass `25000`.
- **`credit_note_create` only works on open invoices.** A paid invoice needs
  `refund_create` instead: Stripe requires a matching refund amount to
  balance a credit note against money that already moved.
- **Void is only for open invoices.** Drafts are deleted, paid invoices
  refunded.
- **One currency per invoice**, defaulting to USD.
- **Subscriptions are cancel-only.** No creating or repricing plans.
- **`payment_method_list` defaults to `type: card`.** Pass a different type
  for anything else; Stripe's list endpoint returns nothing for a type not
  explicitly asked for.
- **`at_period_end` declares no type** in the manifest, because Studio's
  input validator recognises only string, number, array and object. The
  handler itself rejects anything that is not `true` or `false`.
