# Command reference — dave/stripe-invoicing v1.6.0

This index keeps all 34 buyer-facing command IDs
explicit. Every command requires a signed receipt. `read` has no declared
external side effect; `write_requires_approval` means Station Approval Airlock
plus the Stripe idempotency key derived from the approved payload. Inputs and
outputs below use the manifest field names; unspecified defaults are not
invented.

| # | Exact command | Title | Mode / risk | Provider | Inputs (required; optional/default) | Output fields | Side effects, use, failure/caveat |
|---:|---|---|---|---|---|---|---|
|1|`stripe.billing.customer_find`|Find Stripe customers by email|read / low|stripe|email; limit|found, customers, http_status|none; lookup; invalid/provider error fails closed|
|2|`stripe.billing.invoice_list`|List invoices for a customer|read / low|stripe|none; customer_id, status, limit, since, exclude_invoice_ids|found, totals, invoices, since, skipped_already_delivered, truncated, http_status|none; incremental filtering is deterministic; bounded data is explicit|
|3|`stripe.billing.invoice_get`|Retrieve one invoice by id|read / low|stripe|invoice_id|invoice_id, status, amount_due, hosted_invoice_url, http_status|none; provider truth; missing/not-found fails|
|4|`stripe.billing.subscription_list`|List subscriptions for a customer|read / low|stripe|customer_id; status, limit|found, subscriptions, http_status|none; bounded provider list|
|5|`stripe.billing.customer_create`|Create a Stripe customer|write_requires_approval / medium|stripe|email; name, description, phone|customer_id, email, dashboard_url, http_status|external write; approved-payload idempotency; duplicate/provider refusal visible|
|6|`stripe.billing.invoice_create`|Create a draft invoice with line items|write_requires_approval / medium|stripe|customer_id, line_items; currency, days_until_due, description|invoice_id, status, amount_due, line_item_count, dashboard_url, http_status|external write; approval/idempotency; whole-cent validation|
|7|`stripe.billing.invoice_send`|Finalize and email an invoice to the customer|write_requires_approval / high|stripe|invoice_id|invoice_id, status, amount_due, hosted_invoice_url, sent_to, http_status|external send; approval/idempotency; state checked first|
|8|`stripe.billing.invoice_void`|Void a finalized invoice|write_requires_approval / high|stripe|invoice_id|invoice_id, status, amount_voided, http_status|external write; approval/idempotency; Stripe eligibility is authoritative|
|9|`stripe.billing.subscription_cancel`|Cancel a subscription now or at period end|write_requires_approval / high|stripe|subscription_id; at_period_end|subscription_id, status, cancel_at_period_end, ends_at, http_status|external write; approval/idempotency; provider timing is authoritative|
|10|`stripe.billing.customer_summary`|Customer profile, open invoices, subscriptions, and lifetime paid in one call|read / low|stripe|customer_id|profile, exposure, subscription counts, lifetime_paid, http_status|none; composite read; incomplete data is not guessed|
|11|`stripe.billing.aging_report`|Open invoices bucketed by days overdue|read / low|stripe|none; customer_id, limit|found, total_outstanding, buckets, http_status|none; only returned provider invoices are reported|
|12|`stripe.billing.bill_client`|Find or create a customer, then invoice and send in one approval|write_requires_approval / high|stripe|email, description, amount_cents; customer_id, name, currency, days_until_due, billing_run_id, metadata|billing correlation, stages, customer/invoice/send state, URLs, http_status|composed external write; one approved idempotency contract; late ambiguity unknown|
|13|`stripe.billing.invoice_preview`|Preview an invoice locally without Stripe effects|read / low|none|none; email, customer_id, description, amount_cents, line_items, currency, days_until_due, billing_run_id, metadata|preview_only, external_effect, line_items, totals, correlation, note|no network/effect; strict local validation|
|14|`stripe.billing.subscription_renewal_preview`|Preview the next provider-authoritative subscription renewal|read / low|stripe|customer_id; subscription_id, preview_mode (`next`/`recurring`)|financial fields, completeness, unknown_reasons, authority flags, http_status|none; provider preview; ambiguity/truncation reported|
|15|`stripe.billing.payment_status_summary`|Summarize invoice payment status and overdue exposure|read / low|stripe|none; invoice_id, customer_id, limit|invoice rows, open/overdue/partial totals, http_status|none; provider state authoritative|
|16|`stripe.billing.customer_balance_summary`|Summarize customer balance and billing exposure|read / low|stripe|customer_id|provider balance, exposure, paid, invoice/subscription counts, note, http_status|none; currency/exposure ambiguity is preserved|
|17|`stripe.billing.account_preflight`|Assess bounded Stripe billing readiness|read / low|stripe|customer_id; invoice_limit, subscription_limit, payment_method_types (default card)|readiness, exposure, payment method, completeness, unknown_reasons, statuses|none; decision support; missing authoritative fields stay unknown|
|18|`stripe.billing.credit_note_create`|Issue a credit note against a finalized invoice|write_requires_approval / high|stripe|invoice_id, amount_cents; reason, description|credit_note_id, invoice_id, status, amount_credited, http_status|external write; approval/idempotency; integer cents required|
|19|`stripe.billing.refund_create`|Refund a charge (structured output, composes with the rest of this module)|write_requires_approval / high|stripe|charge_id; amount_cents, reason|refund_id, charge_id, status, amount_refunded, http_status|external write; approval/idempotency; provider outcome authoritative|
|20|`stripe.billing.payment_method_list`|List a customer's saved payment methods (brand + last4 only, type configurable)|read / low|stripe|customer_id; type|found, payment_methods, type_queried, http_status|none; minimised payment-method data|
|21|`stripe.billing.coupon_list`|List Stripe coupons|read / low|stripe|none; limit|found, coupons, http_status|none; bounded provider list|
|22|`stripe.billing.coupon_create`|Create a coupon|write_requires_approval / medium|stripe|none; percent_off, amount_cents_off, currency, duration, duration_in_months, name|coupon_id, discount fields, duration, http_status|external write; approval/idempotency; valid discount shape required|
|23|`stripe.billing.promotion_code_create`|Create a promotion code for an existing coupon|write_requires_approval / medium|stripe|coupon_id; code, max_redemptions|promotion_code_id, code, coupon_id, active, http_status|external write; approval/idempotency; coupon must exist|
|24|`stripe.billing.product_create`|Create a product|write_requires_approval / medium|stripe|name; description|product_id, name, active, http_status|external write; approval/idempotency; provider refusal fails closed|
|25|`stripe.billing.product_list`|List products|read / low|stripe|none; limit|found, products, http_status|none; bounded provider list|
|26|`stripe.billing.price_create`|Create a price: one-time, recurring, or metered|write_requires_approval / medium|stripe|product_id, unit_amount_cents; currency, interval, meter_id|price_id, product/type, amount, interval, metered, http_status|external write; approval/idempotency; integer cents/provider rules|
|27|`stripe.billing.price_list`|List prices|read / low|stripe|none; product_id, limit|found, prices, http_status|none; bounded provider list|
|28|`stripe.billing.usage_record_create`|Record usage against a metered price|write_requires_approval / medium|stripe|meter_id, customer_id, value|event_id, meter/customer, value, note, http_status|external write; approval/idempotency; aggregation may lag|
|29|`stripe.billing.usage_summary_list`|Read aggregated usage|read / low|stripe|meter_id, customer_id; start_time, end_time|found, summaries, note, http_status|none; allow roughly 30 seconds provider lag|
|30|`stripe.billing.mandate_get`|Retrieve a mandate|read / low|stripe|mandate_id|mandate, acceptance, http_status|none; mandates cannot be created by this API path|
|31|`stripe.billing.payment_risk_assess`|Assess payment risk from minimised account metrics (AI)|read / low|groq|open_invoice_count, overdue_invoice_count, oldest_overdue_days, outstanding_cents; paid_on_time_count, paid_late_count|risk_level, score, drivers, recommended_review, receipt_id|no Stripe effect; governed egress; malformed AI reply fails closed|
|32|`stripe.billing.collection_strategy_recommend`|Recommend a reviewable collections strategy (AI)|read / low|groq|risk_level, days_overdue, outstanding_cents, prior_reminder_count; dispute_open, payment_commitment_present|urgency, action, wait_days, escalation, rationale, receipt_id|no Stripe effect; advisory only; structured reply validated|
|33|`stripe.billing.billing_anomaly_detect`|Detect anomalies in minimised billing metrics (AI)|read / low|groq|billing_period, portfolio; portfolio_baseline_cents|anomalies, portfolio_risk, review order, receipt_id|no Stripe effect; opaque submitted refs constrain output; invalid reply fails closed|
|34|`stripe.billing.dunning_message_draft`|Draft a privacy-minimised payment reminder (AI)|read / low|groq|amount_due_cents, days_overdue; currency, tone, sender_name|subject, body, receipt_id, decision_support_only, draft_only|no send/charge/Stripe mutation; governed egress; human review required|

For exact types, required flags, and schema descriptions, the manifest is the
authoritative source. Runtime/provider errors are reported in Station receipts;
this reference does not promise a successful provider response.
