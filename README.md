# dave/stripe-invoicing v1.6.0

Governed Stripe billing operations for RailCall Station. The module helps an
operator inspect billing state, prepare or execute approval-controlled billing
effects, and obtain bounded AI decision support. It is for teams that need a
reviewable billing workflow with provider truth, receipts, and deterministic
retries—not an unattended money-moving bot.

## Capability map

The manifest contains exactly 34 commands: 21 `read` commands and 13
`write_requires_approval` commands. Read commands cover customer, invoice,
subscription, payment, product/price, usage, mandate, preflight, summaries,
and four AI decision-support operations. The 13 writes create or change Stripe
objects, issue refunds/credits, send or void invoices, cancel subscriptions,
create catalog objects, or record usage. The complete buyer-facing contract is
in [COMMANDS.md](COMMANDS.md).

Every command requires a signed receipt. Read commands do not declare external
effects. Write commands stop at the Station Approval Airlock and require the
exact approved payload before any Stripe mutation. The handler uses Stripe's
provider response as authority and fails closed when validation, identity,
provider state, or post-effect certainty is insufficient.

## Install and Configure

Install `dave/stripe-invoicing` v1.6.0 through the normal Station module
loader. Configure a Stripe credential with the canonical field:

```text
STRIPE_SECRET_KEY=sk_test_...
```

The effective Station credential namespace is
`dave-stripe-invoicing::stripe`. Users should configure the module through
Station's Configure flow and should not hardcode that namespace in handler
code or workflow inputs. The portable handler lookup is `vault_get("stripe")`;
Station resolves the module namespace around it.

After the v1.6 credential namespace collision fix, a plain legacy `stripe`
entry is not automatically or transparently migrated to the namespaced entry.
Perform the one-time explicit Configure migration, then verify a fresh
credential resolution before running the module. This is intentional: silent
credential fallback could select the wrong secret.

AI commands additionally require the Station-governed Groq path and its
configured credential (`GROQ_API_KEY`) where AI is enabled. AI results are
advisory and never authorize a Stripe write, charge, refund, or invoice send.

Use Stripe TEST credentials for evaluation. Never paste a secret into a
workflow, receipt, log, screenshot, or documentation.

## Safety and operating model

- **Approval:** all 13 provider writes are Airlock-gated; approval is a
  separate human decision and does not happen inside the handler.
- **Idempotency:** writes bind the Stripe `Idempotency-Key` to the approved
  payload hash. Missing approval/idempotency helpers fail before the provider
  call.
- **Provider truth:** returned status, amounts, currency, identity, and
  uncertainty are preserved from Stripe. Local estimates are not presented as
  financial authority.
- **Receipts and governance:** Station owns receipt persistence, signature,
  approval state, and egress policy. Each command requires a receipt.
- **Network boundary:** the manifest permits `api.stripe.com` and
  `api.groq.com`; it declares no subprocess use and no filesystem writes.
- **Errors:** malformed inputs, unsupported credential shapes, missing
  credentials, provider refusal, ambiguous outcomes, malformed AI replies,
  and incomplete bounded reads fail closed with the secret excluded from the
  error text.

## Quick start

1. Install the module at v1.6.0.
2. Configure a fresh `STRIPE_SECRET_KEY` through Station Configure.
3. Use a read or `invoice_preview` command to inspect the intended result.
4. Review the receipt and Airlock payload before approving any write.
5. Run against Stripe TEST data and retain the resulting evidence.

For exact fields, outputs, defaults, caveats, and failure boundaries, see
[COMMANDS.md](COMMANDS.md). For the evidence protocol and known proof limits,
see [TESTING.md](TESTING.md). [STOREFRONT.md](STOREFRONT.md) is the buyer-facing
listing copy; marketplace publication status is separate from this repository.

## Known limitations

This module does not make production-money claims from local tests. Stripe
TEST evidence is not production verification. Provider lag can affect usage
summaries. Bounded reads report unknown or incomplete state instead of
guessing. AI output is structured decision support only. A legacy plain
`stripe` credential requires explicit migration. The current public Module
video is [available on YouTube](https://youtu.be/WSC9mgYl270); the video does
not claim production-money success or marketplace publication.

## Evidence index

- Current test report and exact verification scope: [TESTING.md](TESTING.md).
- Exhaustive command reference: [COMMANDS.md](COMMANDS.md).
- Public Module video: https://youtu.be/WSC9mgYl270.
- Homepage: https://davelab10.github.io/portofolio/.
- Tests: https://github.com/davelab10/railcall-stripe-invoicing/blob/main/TESTING.md.
