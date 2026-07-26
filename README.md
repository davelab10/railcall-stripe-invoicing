# Stripe Invoicing for RailCall

Bill a client and collect, without handing an agent your Stripe account.

## What it does

Nine commands covering the bill-and-collect cycle, split by blast radius. Four
reads run freely: `customer_find`, `invoice_list`, `invoice_get`,
`subscription_list`. Five writes stop at the airlock for explicit human
approval: `customer_create`, `invoice_create`, `invoice_send`, `invoice_void`,
`subscription_cancel`.

Every write that commits money carries a Stripe `Idempotency-Key` derived from
the approved payload hash. One approval is at most one effect — a retry after a
dropped connection returns the original result instead of billing twice.

## Who it is for

The solo consultant or small agency who runs billing from an assistant. You
want "draft July's invoice for Ada and send it" to work in one sentence, but you
do not want a model deciding on its own to email a $10,000 invoice. Reads stay
frictionless; anything that reaches a customer's inbox waits for your click.

## Install

```
railcall market install dave/stripe-invoicing
```

Restart Studio. Startup logs `[modules] loaded=2` with the nine commands.

## Credentials

One Stripe secret key, held locally and sent only as an `Authorization` header —
never in a request body, a payload hash, or a receipt.

**Save it under the legacy `STRIPE_SECRET_KEY` field** in Studio → Integrations →
the stripe card. The newer "Add credential" button writes to a different store
that handlers cannot read, and the module will look broken if you use it. If the
key is missing the module says so in those words rather than failing obscurely.

`sk_test_` keys work end to end.

## Worked example

Draft a two-line invoice, then send it. Both steps require approval.

`stripe.billing.invoice_create`:

```json
{"customer_id": "cus_UxMQ9Kr4nLp3kE",
 "line_items": [{"description": "Analytics retainer — July 2026", "amount_cents": 250000},
                {"description": "Dashboard build (8 hrs)", "amount_cents": 96000, "quantity": 8}],
 "days_until_due": 14}
```

Actual output:

```json
{"ok": true, "http_status": 200,
 "invoice_id": "in_1TxRhuIiIXjQdConWrm1pKv7", "status_at_stripe": "draft",
 "amount_due": "10180.00 USD", "line_item_count": 2,
 "note": "draft only, nothing emailed. Run invoice_send to finalize and deliver."}
```

Then `stripe.billing.invoice_send` with that `invoice_id`:

```json
{"ok": true, "http_status": 200, "invoice_id": "in_1TxRhuIiIXjQdConWrm1pKv7",
 "number": "3I7BVVG1-0001", "status_at_stripe": "open",
 "amount_due": "10180.00 USD", "was_before_send": "draft"}
```

Each run leaves an Ed25519-signed receipt carrying the approved payload hash.

## Known limitations

- **Amounts are integer cents.** `250.00` is rejected; pass `25000`.
- **Quantity is multiplied into the line total.** Stripe's `invoiceitems`
  endpoint no longer accepts `unit_amount` with `quantity`, so `8 × $960` posts
  as one $7,680 line reading `Dashboard build (8 hrs) (x8 @ 960.00 USD)`.
- **No refunds.** The built-in `stripe.create_refund` already covers that.
- **Void is only for open invoices.** Drafts are deleted, paid invoices refunded.
- **One currency per invoice**, defaulting to USD.
- **Subscriptions are cancel-only** — no creating or repricing plans.
- **`at_period_end` declares no type** in the manifest, because Studio's input
  validator recognises only string, number, array and object. The handler itself
  rejects anything that is not `true` or `false`.
