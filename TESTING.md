# Module testing and evidence — v1.6.1

This document records the local Step 3 regression baseline for
`dave/stripe-invoicing` v1.6.1. It is an evidence boundary, not a claim of
production-money success.

## Baseline

- Manifest: v1.6.1, 34 commands, 21 read, 13 approval-controlled writes.
- Regression target: 56 tests, 42 subtests, 0 failures, 0 errors, 0 skips, and
  0 warnings when run in the restored project environment.
- The release signature must verify against the signed module package; this
  public repository does not distribute signing material.
- The canonical Module credential field is `STRIPE_SECRET_KEY`.
- Station resolves it in `dave-stripe-invoicing::stripe`; the handler remains
  portable and calls `vault_get("stripe")`.
- A plain legacy `stripe` credential is deliberately not transparently
  namespaced. Fresh Configure and one-time explicit migration are required.

## Exact checks

Run from the repository root:

```bash
python3 -m pytest -q module/tests/test_module_v130.py
git diff --check
```

The regression exercises registration, schemas, read/write governance,
credential namespace behavior, fail-closed credential shapes, incremental
retrieval, whole-cent and idempotency safeguards, finance summaries, bounded
preflight and renewal preview, and AI response validation. It does not perform
live financial writes.

## What the evidence proves

- All 34 manifest commands resolve to handler functions and require receipts.
- Writes remain approval-gated and use the approved payload for idempotency.
- Invalid inputs and missing helpers stop before unsafe provider effects.
- Credential resolution is explicit and namespace-aware; legacy fallback is not
  silently accepted by Station.
- Stripe TEST fixtures/evidence can validate provider-shaped behavior where
  available. This is not proof of production credentials, production money,
  every live Stripe object, or marketplace publication.
- AI calls are governed egress through the Station path and return structured,
  reviewable advisory output. They cannot authorize a write.
- The manifest network boundary is limited to `api.stripe.com` and
  `api.groq.com`, with no subprocess and no filesystem writes declared.

## What is not proved

No live production charge, refund, invoice send, cancellation, or catalog
mutation is performed. Local tests do not prove account-specific permissions,
provider uptime, payment success, or marketplace publication. Usage aggregation
may lag provider data. The current public Module video is evidence of the
documented product surface, not proof of production-money execution:
https://youtu.be/luhg76zC0n4.

## Public evidence locations

- Command-level contract: [COMMANDS.md](COMMANDS.md)
- Public release documentation: [README.md](README.md)
- Public test URL: https://github.com/davelab10/railcall-stripe-invoicing/blob/main/TESTING.md
