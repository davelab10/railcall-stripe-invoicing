"""dave/stripe-invoicing v1.1.1 - governed Stripe invoicing.

Vault entry `stripe` in keys.local.json:
    { "STRIPE_SECRET_KEY": "sk_test_..." }

The key never leaves this machine. It is read through vault_get at call time
and injected as an Authorization header only, so it never lands in the request
body and never becomes part of the airlock payload hash or the signed receipt.

Nine commands, split by blast radius:

  read  (no approval)   customer_find, invoice_list, invoice_get,
                        subscription_list
  write (airlock gate)  customer_create, invoice_create, invoice_send,
                        invoice_void, subscription_cancel

Design notes worth knowing before you edit this file:

  * Stripe is form-encoded, not JSON. Writes go through http_post_form.
    Reads are plain GETs with a query string, so http_get_json is fine even
    though Stripe is not a JSON-body API on the way in.

  * Amounts are integer cents everywhere on input, matching the convention
    the built-in stripe.create_refund handler already uses. Outputs carry a
    formatted string as well so a receipt reads like money instead of like
    an integer.

  * Every write that can move or commit money sends a Stripe Idempotency-Key
    built from the airlock payload hash. One approval equals at most one
    effect, even if the network drops after Stripe already accepted the call.

  * The HTTP helpers raise RuntimeError on any 4xx or 5xx, with the raw Stripe
    body glued onto the message. Raw Stripe errors are not helpful to an
    operator, so _call unwraps them into something actionable. See _explain.
"""

import json as _json
import urllib.parse as _urlparse

STRIPE_API = "https://api.stripe.com/v1"

# Invoice states Stripe will accept for each transition. Checking locally
# before we call turns a confusing 400 into a sentence the operator can act on.
_SENDABLE = ("draft", "open")
_VOIDABLE = ("open",)
# Verified live against Stripe test mode. draft: "You cannot create a credit
# note for a draft invoice." paid: accepted the call, but rejected the plain
# custom_line_item shape with "The sum of refunds, credit amount, and out of
# band amount ($0.00) must equal the credit note post_payment_amount" -- a
# credit note against money that already moved needs a matching refund_amount
# or out_of_band_amount, which is really the refund_create job. So this stays
# open-only; paid routes the operator to refund_create instead.
_CREDIT_NOTE_ELIGIBLE = ("open",)


# ---------------------------------------------------------------- credentials

def _api_key():
    """Read the Stripe secret from the local vault.

    Field name is not consistent across the RailCall codebase: the Integrations
    tab writes STRIPE_SECRET_KEY, while the built-in refund handler looks for
    api_key or secret_key. We accept all three so the module works no matter
    which path the operator used to save the key.
    """
    helpers = __rc_helpers__  # noqa: F821
    entry = helpers["vault_get"]("stripe")

    key = ""
    if isinstance(entry, dict):
        for field in ("STRIPE_SECRET_KEY", "api_key", "secret_key", "token"):
            value = entry.get(field)
            if isinstance(value, str) and value.strip():
                key = value.strip()
                break
    elif isinstance(entry, str):
        key = entry.strip()

    if not key:
        raise RuntimeError(
            "no Stripe key in the vault. Open Studio, go to Integrations, find "
            "the stripe card, and save your key under the legacy STRIPE_SECRET_KEY "
            "field. Note that the newer Add credential button writes to a "
            "different store that handlers cannot read."
        )
    if not key.startswith(("sk_", "rk_")):
        raise RuntimeError(
            "that does not look like a Stripe secret key. Expected sk_test_, "
            "sk_live_, or a restricted rk_ key. A pk_ publishable key cannot "
            "create invoices."
        )
    return key


# ------------------------------------------------------------------ transport

def _explain(err_text, context):
    """Turn a raw Stripe error into something an operator can act on.

    err_text arrives as "HTTP 404: {json}" from the helper. Stripe puts the
    useful part in error.message and error.code, so we dig those out and add
    the fix where the fix is not obvious from the message alone.
    """
    code = ""
    message = err_text
    status = ""

    head, _, body = err_text.partition(":")
    if head.startswith("HTTP "):
        status = head[5:].strip()
    try:
        payload = _json.loads(body.strip())
        error = payload.get("error") or {}
        message = error.get("message") or message
        code = error.get("code") or error.get("type") or ""
    except Exception:
        pass

    if status == "401":
        return RuntimeError(
            "Stripe rejected the key (401). It is revoked, mistyped, or from a "
            "different account. Re-save it in Studio under Integrations."
        )
    if status == "429":
        return RuntimeError(
            "Stripe rate limited this call (429). Nothing was created. Wait a "
            "few seconds and approve again, the idempotency key makes a retry safe."
        )
    if code == "resource_missing":
        return RuntimeError(
            "Stripe has no such object: " + message + ". Check the id, and check "
            "you are not mixing a test-mode id with a live key or the reverse."
        )
    if status and status.startswith("5"):
        return RuntimeError(
            "Stripe had a server error (" + status + "). This is on their side. "
            "The call may or may not have landed, so retry rather than recreate."
        )
    return RuntimeError("Stripe " + context + " failed: " + message)


def _call(method, path, form=None, idem_for=None, inputs=None, stamp=""):
    """One entry point for every Stripe call so error handling stays uniform."""
    helpers = __rc_helpers__  # noqa: F821
    key = _api_key()
    headers = {"Authorization": "Bearer " + key}

    # Idempotency only matters on writes. The airlock hash ties the key to the
    # exact payload that was approved, so a retry of the same approved action
    # is a no-op at Stripe rather than a second charge.
    if idem_for:
        try:
            headers["Idempotency-Key"] = helpers["airlock_payload_hash"](
                idem_for, inputs or {}
            )
        except Exception:
            raise RuntimeError(
                "station too old, Stripe idempotency requires the "
                "airlock_payload_hash helper (v0.22+). Run: railcall update"
            )

    url = STRIPE_API + path
    try:
        if method == "POST":
            status, raw = helpers["http_post_form"](
                url, form or {}, timeout=25, headers=headers
            )
        elif method == "DELETE":
            status, raw = helpers["http_delete_json"](url, timeout=25, headers=headers)
        else:
            if form:
                url = url + "?" + _urlparse.urlencode(form)
            status, raw = helpers["http_get_json"](url, timeout=25, headers=headers)
    except RuntimeError as exc:
        raise _explain(str(exc), path.strip("/"))

    try:
        parsed = _json.loads(raw.decode("utf-8"))
    except Exception:
        parsed = {}
    return status, parsed


# --------------------------------------------------------------------- shared

def _money(cents, currency="usd"):
    """Format integer cents for a human reading a receipt."""
    try:
        return "{:.2f} {}".format(int(cents) / 100.0, str(currency).upper())
    except Exception:
        return str(cents)


def _need_str(inputs, field, hint=""):
    value = inputs.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(field + " must be a non-empty string" + (". " + hint if hint else ""))
    return value.strip()


def _clamp_limit(inputs, default=10):
    raw = inputs.get("limit")
    if raw is None:
        return default
    try:
        limit = int(raw)
    except Exception:
        raise RuntimeError("limit must be a whole number")
    return max(1, min(limit, 100))


def _period_end(subscription):
    """Stripe reports the billing period on the subscription ITEM now; the
    top-level current_period_end comes back null."""
    items = (subscription.get("items") or {}).get("data") or []
    if items and items[0].get("current_period_end"):
        return items[0]["current_period_end"]
    return subscription.get("current_period_end")


def _dashboard(kind, object_id, key):
    """Test-mode objects live under a different dashboard path than live ones."""
    prefix = "https://dashboard.stripe.com/"
    if key.startswith(("sk_test", "rk_test")):
        prefix += "test/"
    return prefix + kind + "/" + object_id


# ----------------------------------------------------------------------- read

def stripe_billing_customer_find(inputs, stamp):
    email = _need_str(inputs, "email")
    if "@" not in email:
        raise RuntimeError("email must contain '@'")

    status, parsed = _call(
        "GET", "/customers", {"email": email, "limit": _clamp_limit(inputs)}
    )
    rows = parsed.get("data") or []
    customers = [
        {
            "customer_id": row.get("id"),
            "email": row.get("email"),
            "name": row.get("name") or "",
            "delinquent": bool(row.get("delinquent")),
            "created": row.get("created"),
        }
        for row in rows
    ]
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(customers),
        "customers": customers,
        "searched_email": email,
    }, None


def stripe_billing_invoice_list(inputs, stamp):
    form = {"limit": _clamp_limit(inputs)}

    customer_id = inputs.get("customer_id")
    if isinstance(customer_id, str) and customer_id.strip():
        form["customer"] = customer_id.strip()

    wanted = inputs.get("status")
    if wanted:
        wanted = str(wanted).strip().lower()
        allowed = ("draft", "open", "paid", "uncollectible", "void")
        if wanted not in allowed:
            raise RuntimeError("status must be one of: " + ", ".join(allowed))
        form["status"] = wanted

    status, parsed = _call("GET", "/invoices", form)
    rows = parsed.get("data") or []

    outstanding = 0
    invoices = []
    for row in rows:
        due = row.get("amount_due") or 0
        currency = row.get("currency") or "usd"
        if row.get("status") == "open":
            outstanding += due
        invoices.append({
            "invoice_id": row.get("id"),
            "number": row.get("number") or "",
            "status": row.get("status"),
            "amount_due": _money(due, currency),
            "amount_due_cents": due,
            "customer_id": row.get("customer"),
            "customer_email": row.get("customer_email") or "",
            "due_date": row.get("due_date"),
            "hosted_invoice_url": row.get("hosted_invoice_url") or "",
        })

    currency = rows[0].get("currency") if rows else "usd"
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(invoices),
        "total_outstanding": _money(outstanding, currency),
        "total_outstanding_cents": outstanding,
        "invoices": invoices,
    }, None


def stripe_billing_invoice_get(inputs, stamp):
    invoice_id = _need_str(inputs, "invoice_id", "Stripe invoice ids start with in_")
    status, parsed = _call("GET", "/invoices/" + invoice_id)
    currency = parsed.get("currency") or "usd"
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "invoice_id": parsed.get("id"),
        "number": parsed.get("number") or "",
        "status": parsed.get("status"),
        "amount_due": _money(parsed.get("amount_due") or 0, currency),
        "amount_paid": _money(parsed.get("amount_paid") or 0, currency),
        "customer_id": parsed.get("customer"),
        "customer_email": parsed.get("customer_email") or "",
        "hosted_invoice_url": parsed.get("hosted_invoice_url") or "",
        "line_item_count": len((parsed.get("lines") or {}).get("data") or []),
    }, None


def stripe_billing_subscription_list(inputs, stamp):
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")
    form = {"customer": customer_id, "limit": _clamp_limit(inputs)}

    wanted = inputs.get("status")
    if wanted:
        wanted = str(wanted).strip().lower()
        allowed = ("active", "past_due", "canceled", "trialing", "unpaid", "all")
        if wanted not in allowed:
            raise RuntimeError("status must be one of: " + ", ".join(allowed))
        form["status"] = wanted

    status, parsed = _call("GET", "/subscriptions", form)
    rows = parsed.get("data") or []
    subscriptions = []
    for row in rows:
        items = (row.get("items") or {}).get("data") or []
        first = items[0] if items else {}
        plan = first.get("price") or {}
        subscriptions.append({
            "subscription_id": row.get("id"),
            "status": row.get("status"),
            "amount": _money(plan.get("unit_amount") or 0, plan.get("currency") or "usd"),
            "interval": (plan.get("recurring") or {}).get("interval") or "",
            "current_period_end": _period_end(row),
            "cancel_at_period_end": bool(row.get("cancel_at_period_end")),
        })

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(subscriptions),
        "subscriptions": subscriptions,
        "customer_id": customer_id,
    }, None


# ---------------------------------------------------------------------- write

def stripe_billing_customer_create(inputs, stamp):
    email = _need_str(inputs, "email")
    if "@" not in email:
        raise RuntimeError("email must contain '@'")

    form = {"email": email}
    for field in ("name", "description", "phone"):
        value = inputs.get(field)
        if isinstance(value, str) and value.strip():
            form[field] = value.strip()

    status, parsed = _call(
        "POST", "/customers", form,
        idem_for="stripe.billing.customer_create", inputs=inputs, stamp=stamp,
    )
    customer_id = parsed.get("id") or ""
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "customer_id": customer_id,
        "email": parsed.get("email") or email,
        "name": parsed.get("name") or "",
        "dashboard_url": _dashboard("customers", customer_id, _api_key()) if customer_id else "",
    }, None


def stripe_billing_invoice_create(inputs, stamp):
    """Create a draft invoice.

    Stripe builds invoices in two steps: an invoice, and the items on it. We do
    both here because an operator thinks in terms of one document, not two API
    objects. The draft is opened first and every item is attached to it by id.
    The alternative — letting a new invoice sweep up whatever pending items are
    sitting on the customer — would silently bill anything another tool had
    queued against the same customer.

    The invoice comes out as a draft on purpose. Nothing is emailed and no
    money is requested until invoice_send runs, which is a separate approval.
    """
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")

    line_items = inputs.get("line_items")
    if not isinstance(line_items, list) or not line_items:
        raise RuntimeError(
            "line_items must be a non-empty list, for example "
            '[{"description": "Design work", "amount_cents": 25000}]'
        )

    currency = str(inputs.get("currency") or "usd").strip().lower()

    # Validate the whole batch before creating anything. A half-built invoice
    # with three of five items attached is worse than a clean refusal.
    cleaned = []
    for index, item in enumerate(line_items):
        where = "line_items[" + str(index) + "]"
        if not isinstance(item, dict):
            raise RuntimeError(where + " must be an object with description and amount_cents")
        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            raise RuntimeError(where + ".description must be a non-empty string")
        try:
            amount = int(item.get("amount_cents"))
        except Exception:
            raise RuntimeError(
                where + ".amount_cents must be a whole number of cents. "
                "250.00 dollars is 25000."
            )
        if amount <= 0:
            raise RuntimeError(where + ".amount_cents must be greater than zero")
        # `or 1` would read a deliberate 0 as "not supplied" and bill one unit
        # anyway. On a money field an unasked-for quantity is worse than a refusal.
        raw_quantity = item.get("quantity")
        if raw_quantity is None:
            quantity = 1
        else:
            try:
                quantity = int(raw_quantity)
            except Exception:
                raise RuntimeError(where + ".quantity must be a whole number")
        if quantity <= 0:
            raise RuntimeError(where + ".quantity must be greater than zero")
        cleaned.append({
            "description": description.strip(),
            "amount": amount,
            "quantity": quantity,
        })

    form = {"customer": customer_id, "collection_method": "send_invoice"}
    raw_due = inputs.get("days_until_due")
    if raw_due is None:
        form["days_until_due"] = "30"
    else:
        # Same trap as quantity: days_until_due=0 means due on issue, not "use
        # the default 30 days".
        try:
            days = int(raw_due)
        except Exception:
            raise RuntimeError("days_until_due must be a whole number of days")
        if days < 0:
            raise RuntimeError("days_until_due cannot be negative")
        form["days_until_due"] = str(days)

    description = inputs.get("description")
    if isinstance(description, str) and description.strip():
        form["description"] = description.strip()

    status, parsed = _call(
        "POST", "/invoices", form,
        idem_for="stripe.billing.invoice_create", inputs=inputs, stamp=stamp,
    )
    invoice_id = parsed.get("id") or ""

    for index, item in enumerate(cleaned):
        # /v1/invoiceitems rejects unit_amount + quantity ("Received unknown
        # parameter: unit_amount"), and its price_data needs a pre-existing
        # product id. Only a flat `amount` is accepted, so the line total is
        # multiplied out here and the quantity is kept in the description so a
        # human reading the invoice still sees how the number was reached.
        label = item["description"]
        if item["quantity"] > 1:
            label += " (x" + str(item["quantity"]) + " @ " + _money(item["amount"], currency) + ")"
        _call(
            "POST", "/invoiceitems",
            {
                "customer": customer_id,
                "invoice": invoice_id,
                "description": label,
                "amount": str(item["amount"] * item["quantity"]),
                "currency": currency,
            },
            # Index the idempotency scope so five distinct items do not collapse
            # into one under a shared payload hash.
            idem_for="stripe.billing.invoice_create.item." + str(index),
            inputs=inputs, stamp=stamp,
        )

    # Re-read rather than trusting the create response: the totals only exist
    # once the items are attached.
    _, final = _call("GET", "/invoices/" + invoice_id)
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "invoice_id": invoice_id,
        "status_at_stripe": final.get("status") or parsed.get("status"),
        "amount_due": _money(final.get("amount_due") or 0, final.get("currency") or currency),
        "line_item_count": len(cleaned),
        "customer_id": customer_id,
        "dashboard_url": _dashboard("invoices", invoice_id, _api_key()) if invoice_id else "",
        "note": "draft only, nothing emailed. Run invoice_send to finalize and deliver.",
    }, None


def stripe_billing_invoice_send(inputs, stamp):
    """Finalize and email an invoice.

    This is the command that reaches a human inbox, so it checks state first.
    Stripe's own error for sending an already paid invoice is vague, and an
    operator who sees it has no idea whether the customer was emailed twice.
    """
    invoice_id = _need_str(inputs, "invoice_id", "Stripe invoice ids start with in_")

    _, current = _call("GET", "/invoices/" + invoice_id)
    state = current.get("status")
    if state not in _SENDABLE:
        raise RuntimeError(
            "invoice " + invoice_id + " is " + str(state) + ", so it cannot be sent. "
            "Only draft or open invoices can go out. A paid invoice needs no send, "
            "and a void one has to be recreated."
        )

    status, parsed = _call(
        "POST", "/invoices/" + invoice_id + "/send",
        {},
        idem_for="stripe.billing.invoice_send", inputs=inputs, stamp=stamp,
    )
    currency = parsed.get("currency") or "usd"
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "invoice_id": parsed.get("id") or invoice_id,
        "number": parsed.get("number") or "",
        "status_at_stripe": parsed.get("status"),
        "amount_due": _money(parsed.get("amount_due") or 0, currency),
        "sent_to": parsed.get("customer_email") or "",
        "hosted_invoice_url": parsed.get("hosted_invoice_url") or "",
        "was_before_send": state,
    }, None


def stripe_billing_invoice_void(inputs, stamp):
    """Void a finalized invoice. Permanent, and the number is retired."""
    invoice_id = _need_str(inputs, "invoice_id", "Stripe invoice ids start with in_")

    _, current = _call("GET", "/invoices/" + invoice_id)
    state = current.get("status")
    if state not in _VOIDABLE:
        hint = {
            "draft": "a draft was never finalized, delete it instead of voiding",
            "paid": "a paid invoice cannot be voided, issue a refund instead",
            "void": "this invoice is already void",
            "uncollectible": "mark it uncollectible rather than void",
        }.get(state, "only open invoices can be voided")
        raise RuntimeError("invoice " + invoice_id + " is " + str(state) + ". " + hint + ".")

    amount = current.get("amount_due") or 0
    currency = current.get("currency") or "usd"

    status, parsed = _call(
        "POST", "/invoices/" + invoice_id + "/void",
        {},
        idem_for="stripe.billing.invoice_void", inputs=inputs, stamp=stamp,
    )
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "invoice_id": parsed.get("id") or invoice_id,
        "number": parsed.get("number") or "",
        "status_at_stripe": parsed.get("status"),
        "amount_voided": _money(amount, currency),
        "customer_id": parsed.get("customer"),
    }, None


def stripe_billing_subscription_cancel(inputs, stamp):
    """Cancel a subscription.

    Two very different outcomes hide behind one word. Cancelling now stops
    access immediately, cancelling at period end lets the customer keep what
    they already paid for. Defaulting to period end is the kinder failure mode
    if the operator did not think about it.
    """
    subscription_id = _need_str(
        inputs, "subscription_id", "Stripe subscription ids start with sub_"
    )
    at_period_end = inputs.get("at_period_end")
    if at_period_end is None:
        at_period_end = True
    if not isinstance(at_period_end, bool):
        raise RuntimeError("at_period_end must be true or false")

    if at_period_end:
        status, parsed = _call(
            "POST", "/subscriptions/" + subscription_id,
            {"cancel_at_period_end": "true"},
            idem_for="stripe.billing.subscription_cancel", inputs=inputs, stamp=stamp,
        )
    else:
        status, parsed = _call("DELETE", "/subscriptions/" + subscription_id)

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "subscription_id": parsed.get("id") or subscription_id,
        "status_at_stripe": parsed.get("status"),
        "cancel_at_period_end": bool(parsed.get("cancel_at_period_end")),
        "ends_at": _period_end(parsed) or parsed.get("canceled_at") or "",
        "customer_id": parsed.get("customer"),
        "mode": "at period end" if at_period_end else "immediate",
    }, None


# ---------------------------------------------------------- v1.1 additions

def stripe_billing_customer_summary(inputs, stamp):
    """One call standing in for customer_find + invoice_list + subscription_list.

    Lifetime paid is capped at the most recent 100 paid invoices, same
    Stripe-imposed page limit the other list commands live with.
    """
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")

    status, customer = _call("GET", "/customers/" + customer_id)
    _, open_invoices = _call("GET", "/invoices", {"customer": customer_id, "status": "open", "limit": 100})
    _, paid_invoices = _call("GET", "/invoices", {"customer": customer_id, "status": "paid", "limit": 100})
    _, subs = _call("GET", "/subscriptions", {"customer": customer_id, "status": "active", "limit": 100})

    open_rows = open_invoices.get("data") or []
    paid_rows = paid_invoices.get("data") or []
    currency = (open_rows[0].get("currency") if open_rows else
                paid_rows[0].get("currency") if paid_rows else "usd")

    outstanding = sum(row.get("amount_due") or 0 for row in open_rows)
    lifetime = sum(row.get("amount_paid") or 0 for row in paid_rows)
    capped = len(paid_rows) >= 100

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "customer_id": customer.get("id") or customer_id,
        "email": customer.get("email") or "",
        "name": customer.get("name") or "",
        "delinquent": bool(customer.get("delinquent")),
        "open_invoice_count": len(open_rows),
        "total_outstanding": _money(outstanding, currency),
        "active_subscription_count": len((subs.get("data")) or []),
        "lifetime_paid": _money(lifetime, currency),
        "lifetime_paid_note": (
            "capped at the most recent 100 paid invoices" if capped
            else "covers every paid invoice on this customer"
        ),
    }, None


def stripe_billing_aging_report(inputs, stamp):
    """Bucket open invoices by days overdue: 0-30, 31-60, 61-90, 90+, plus a
    not-yet-due bucket so the totals reconcile with total_outstanding.
    """
    form = {"status": "open", "limit": _clamp_limit(inputs, default=100)}
    customer_id = inputs.get("customer_id")
    if isinstance(customer_id, str) and customer_id.strip():
        form["customer"] = customer_id.strip()

    status, parsed = _call("GET", "/invoices", form)
    rows = parsed.get("data") or []
    now = time.time()  # noqa: F821 -- pre-injected per the module loader

    names = ("not_yet_due", "0-30", "31-60", "61-90", "90+")
    buckets = {name: {"count": 0, "total_due_cents": 0, "invoices": []} for name in names}
    total_outstanding = 0
    currency = "usd"

    for row in rows:
        due = row.get("amount_due") or 0
        total_outstanding += due
        currency = row.get("currency") or currency
        due_date = row.get("due_date")
        if due_date and due_date < now:
            days_overdue = int((now - due_date) // 86400)
            if days_overdue <= 30:
                name = "0-30"
            elif days_overdue <= 60:
                name = "31-60"
            elif days_overdue <= 90:
                name = "61-90"
            else:
                name = "90+"
        else:
            days_overdue = 0
            name = "not_yet_due"
        bucket = buckets[name]
        bucket["count"] += 1
        bucket["total_due_cents"] += due
        bucket["invoices"].append({
            "invoice_id": row.get("id"),
            "customer_id": row.get("customer"),
            "amount_due": _money(due, row.get("currency") or currency),
            "due_date": due_date,
            "days_overdue": days_overdue,
        })

    for bucket in buckets.values():
        bucket["total_due"] = _money(bucket["total_due_cents"], currency)

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(rows),
        "total_outstanding": _money(total_outstanding, currency),
        "buckets": buckets,
    }, None


def stripe_billing_bill_client(inputs, stamp):
    """Find or create the customer, draft a one-line invoice, finalize and
    send it. One approval for the whole chain: the single command a solo
    consultant actually touches on a normal day.

    Self-contained rather than sharing code with invoice_create, deliberately:
    invoice_create's behaviour is frozen for v1.1, so this does not touch it.
    """
    email = _need_str(inputs, "email")
    if "@" not in email:
        raise RuntimeError("email must contain '@'")
    description = _need_str(inputs, "description")
    try:
        amount = int(inputs.get("amount_cents"))
    except Exception:
        raise RuntimeError(
            "amount_cents must be a whole number of cents. 250.00 dollars is 25000."
        )
    if amount <= 0:
        raise RuntimeError("amount_cents must be greater than zero")
    currency = str(inputs.get("currency") or "usd").strip().lower()

    raw_due = inputs.get("days_until_due")
    if raw_due is None:
        days_form = "30"
    else:
        try:
            days = int(raw_due)
        except Exception:
            raise RuntimeError("days_until_due must be a whole number of days")
        if days < 0:
            raise RuntimeError("days_until_due cannot be negative")
        days_form = str(days)

    _, found = _call("GET", "/customers", {"email": email, "limit": 5})
    rows = found.get("data") or []
    customer_id = ""
    for row in rows:
        if isinstance(row.get("id"), str):
            customer_id = row["id"]
            break

    customer_created = False
    if not customer_id:
        form = {"email": email}
        name = inputs.get("name")
        if isinstance(name, str) and name.strip():
            form["name"] = name.strip()
        _, parsed = _call(
            "POST", "/customers", form,
            idem_for="stripe.billing.bill_client.customer", inputs=inputs, stamp=stamp,
        )
        customer_id = parsed.get("id") or ""
        customer_created = True
        if not customer_id:
            raise RuntimeError("Stripe did not return a customer id for the new customer")

    invoice_form = {"customer": customer_id, "collection_method": "send_invoice",
                    "days_until_due": days_form, "description": description}
    _, invoice = _call(
        "POST", "/invoices", invoice_form,
        idem_for="stripe.billing.bill_client.invoice", inputs=inputs, stamp=stamp,
    )
    invoice_id = invoice.get("id") or ""

    _call(
        "POST", "/invoiceitems",
        {
            "customer": customer_id,
            "invoice": invoice_id,
            "description": description,
            "amount": str(amount),
            "currency": currency,
        },
        idem_for="stripe.billing.bill_client.item", inputs=inputs, stamp=stamp,
    )

    # Freshly created within this same call, so it is always draft; no need
    # to re-check state the way invoice_send does for an id an operator
    # could hand in stale. The send response itself carries the final totals.
    status, sent = _call(
        "POST", "/invoices/" + invoice_id + "/send", {},
        idem_for="stripe.billing.bill_client.send", inputs=inputs, stamp=stamp,
    )
    sent_currency = sent.get("currency") or currency
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "customer_id": customer_id,
        "customer_created": customer_created,
        "invoice_id": sent.get("id") or invoice_id,
        "status_at_stripe": sent.get("status"),
        "amount_due": _money(sent.get("amount_due") or 0, sent_currency),
        "sent_to": sent.get("customer_email") or email,
        "hosted_invoice_url": sent.get("hosted_invoice_url") or "",
        "dashboard_url": _dashboard("invoices", invoice_id, _api_key()) if invoice_id else "",
    }, None


def stripe_billing_credit_note_create(inputs, stamp):
    """Issue a credit note against a finalized invoice.

    void erases an invoice that should never have existed; a refund reverses
    money that already moved. Neither reduces what is owed on a still-live
    invoice after the fact, which is what a credit note is for.
    """
    invoice_id = _need_str(inputs, "invoice_id", "Stripe invoice ids start with in_")
    try:
        amount = int(inputs.get("amount_cents"))
    except Exception:
        raise RuntimeError(
            "amount_cents must be a whole number of cents. 250.00 dollars is 25000."
        )
    if amount <= 0:
        raise RuntimeError("amount_cents must be greater than zero")

    allowed_reasons = ("duplicate", "fraudulent", "order_change", "product_unsatisfactory")
    reason = inputs.get("reason")
    if reason:
        reason = str(reason).strip().lower()
        if reason not in allowed_reasons:
            raise RuntimeError("reason must be one of: " + ", ".join(allowed_reasons))

    _, current = _call("GET", "/invoices/" + invoice_id)
    state = current.get("status")
    if state not in _CREDIT_NOTE_ELIGIBLE:
        hint = {
            "draft": "a draft was never finalized, edit the draft directly instead",
            "paid": "the money already moved, use refund_create instead",
            "void": "a void invoice has nothing left to credit",
            "uncollectible": "this invoice is already written off",
        }.get(state, "only open invoices can receive a credit note")
        raise RuntimeError("invoice " + invoice_id + " is " + str(state) + ". " + hint + ".")

    currency = current.get("currency") or "usd"
    description = inputs.get("description")
    label = description.strip() if isinstance(description, str) and description.strip() else "Credit"

    form = {
        "invoice": invoice_id,
        "lines[0][type]": "custom_line_item",
        "lines[0][description]": label,
        "lines[0][unit_amount]": str(amount),
        "lines[0][quantity]": "1",
    }
    if reason:
        form["reason"] = reason

    status, parsed = _call(
        "POST", "/credit_notes", form,
        idem_for="stripe.billing.credit_note_create", inputs=inputs, stamp=stamp,
    )
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "credit_note_id": parsed.get("id") or "",
        "invoice_id": parsed.get("invoice") or invoice_id,
        "status_at_stripe": parsed.get("status"),
        "amount_credited": _money(parsed.get("amount") or amount, parsed.get("currency") or currency),
    }, None


def stripe_billing_refund_create(inputs, stamp):
    """Refund a charge. A built-in stripe.create_refund already exists with a
    different id (stripe.create_refund); this one returns the same structured
    shape (_money + _cents style) the rest of this module uses, so it composes
    with invoice_list / customer_summary rather than returning raw Stripe JSON.
    """
    charge_id = _need_str(inputs, "charge_id", "a Stripe charge id (ch_...) or payment intent id (pi_...)")

    allowed_reasons = ("duplicate", "fraudulent", "requested_by_customer")
    reason = inputs.get("reason")
    if reason:
        reason = str(reason).strip().lower()
        if reason not in allowed_reasons:
            raise RuntimeError("reason must be one of: " + ", ".join(allowed_reasons))

    form = {"charge": charge_id} if charge_id.startswith("ch_") else {"payment_intent": charge_id}
    amount_cents = inputs.get("amount_cents")
    if amount_cents is not None:
        try:
            amt = int(amount_cents)
        except Exception:
            raise RuntimeError("amount_cents must be a whole number of cents")
        if amt <= 0:
            raise RuntimeError("amount_cents must be greater than zero")
        form["amount"] = str(amt)
    if reason:
        form["reason"] = reason

    status, parsed = _call(
        "POST", "/refunds", form,
        idem_for="stripe.billing.refund_create", inputs=inputs, stamp=stamp,
    )
    currency = parsed.get("currency") or "usd"
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "refund_id": parsed.get("id") or "",
        "charge_id": parsed.get("charge") or charge_id,
        "status_at_stripe": parsed.get("status"),
        "amount_refunded": _money(parsed.get("amount") or 0, currency),
    }, None


def stripe_billing_payment_method_list(inputs, stamp):
    """List a customer's saved payment methods. type defaults to card, the
    common case, but is not hardcoded: pass "sepa_debit", "us_bank_account",
    etc for anything else, since Stripe's list endpoint requires a type and
    returns an empty result for any type not explicitly asked for.
    """
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")
    pm_type = inputs.get("type")
    pm_type = str(pm_type).strip().lower() if isinstance(pm_type, str) and pm_type.strip() else "card"

    status, parsed = _call("GET", "/payment_methods", {"customer": customer_id, "type": pm_type})
    rows = parsed.get("data") or []
    methods = []
    for row in rows:
        card = row.get("card") or {}
        methods.append({
            "payment_method_id": row.get("id"),
            "type": row.get("type"),
            "brand": card.get("brand") or "",
            "last4": card.get("last4") or "",
            "exp_month": card.get("exp_month"),
            "exp_year": card.get("exp_year"),
        })
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(methods),
        "payment_methods": methods,
        "type_queried": pm_type,
    }, None


def stripe_billing_coupon_list(inputs, stamp):
    """List Stripe coupons available to apply via promotion_code_create."""
    status, parsed = _call("GET", "/coupons", {"limit": _clamp_limit(inputs)})
    rows = parsed.get("data") or []
    coupons = []
    for row in rows:
        coupons.append({
            "coupon_id": row.get("id"),
            "name": row.get("name") or "",
            "percent_off": row.get("percent_off"),
            "amount_off": _money(row.get("amount_off"), row.get("currency") or "usd") if row.get("amount_off") else "",
            "duration": row.get("duration"),
            "valid": bool(row.get("valid")),
        })
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(coupons),
        "coupons": coupons,
    }, None


def stripe_billing_coupon_create(inputs, stamp):
    """Create a coupon. Exactly one of percent_off or amount_cents_off must be
    given, since Stripe rejects a coupon carrying both or neither.
    """
    percent_off = inputs.get("percent_off")
    amount_off = inputs.get("amount_cents_off")
    if (percent_off is None) == (amount_off is None):
        raise RuntimeError(
            "give exactly one of percent_off or amount_cents_off, not both and not neither"
        )

    form = {"duration": str(inputs.get("duration") or "once").strip().lower()}
    allowed_durations = ("once", "repeating", "forever")
    if form["duration"] not in allowed_durations:
        raise RuntimeError("duration must be one of: " + ", ".join(allowed_durations))
    if form["duration"] == "repeating":
        try:
            months = int(inputs.get("duration_in_months"))
        except Exception:
            raise RuntimeError("duration_in_months is required and must be a whole number when duration is repeating")
        if months <= 0:
            raise RuntimeError("duration_in_months must be greater than zero")
        form["duration_in_months"] = str(months)

    if percent_off is not None:
        try:
            pct = float(percent_off)
        except Exception:
            raise RuntimeError("percent_off must be a number")
        if not (0 < pct <= 100):
            raise RuntimeError("percent_off must be greater than 0 and at most 100")
        form["percent_off"] = str(pct)
        currency = "usd"
    else:
        try:
            cents = int(amount_off)
        except Exception:
            raise RuntimeError("amount_cents_off must be a whole number of cents")
        if cents <= 0:
            raise RuntimeError("amount_cents_off must be greater than zero")
        currency = str(inputs.get("currency") or "usd").strip().lower()
        form["amount_off"] = str(cents)
        form["currency"] = currency

    name = inputs.get("name")
    if isinstance(name, str) and name.strip():
        form["name"] = name.strip()

    status, parsed = _call(
        "POST", "/coupons", form,
        idem_for="stripe.billing.coupon_create", inputs=inputs, stamp=stamp,
    )
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "coupon_id": parsed.get("id") or "",
        "name": parsed.get("name") or "",
        "percent_off": parsed.get("percent_off"),
        "amount_off": _money(parsed.get("amount_off"), parsed.get("currency") or currency) if parsed.get("amount_off") else "",
        "duration": parsed.get("duration"),
    }, None


def stripe_billing_promotion_code_create(inputs, stamp):
    """Create a promotion code for an existing coupon. Run coupon_list or
    coupon_create first to get a coupon_id.
    """
    coupon_id = _need_str(inputs, "coupon_id", "run coupon_list or coupon_create to get one")

    # /v1/promotion_codes rejects a flat `coupon` param ("Received unknown
    # parameter: coupon"). It wants a nested `promotion` object with a type
    # discriminator, same pattern as invoiceitems' price_data: `coupon` alone
    # is not enough, `promotion[type]=coupon` is required alongside it.
    form = {"promotion[type]": "coupon", "promotion[coupon]": coupon_id}
    code = inputs.get("code")
    if isinstance(code, str) and code.strip():
        form["code"] = code.strip()
    max_redemptions = inputs.get("max_redemptions")
    if max_redemptions is not None:
        try:
            n = int(max_redemptions)
        except Exception:
            raise RuntimeError("max_redemptions must be a whole number")
        if n <= 0:
            raise RuntimeError("max_redemptions must be greater than zero")
        form["max_redemptions"] = str(n)

    status, parsed = _call(
        "POST", "/promotion_codes", form,
        idem_for="stripe.billing.promotion_code_create", inputs=inputs, stamp=stamp,
    )
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "promotion_code_id": parsed.get("id") or "",
        "code": parsed.get("code") or "",
        "coupon_id": coupon_id,
        "active": bool(parsed.get("active")),
    }, None
