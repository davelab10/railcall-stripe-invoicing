"""dave/stripe-invoicing v1.4.0 - governed Stripe receivables.

Vault entry `stripe` in keys.local.json:
    { "STRIPE_SECRET_KEY": "sk_test_..." }

The key never leaves this machine. It is read through vault_get at call time
and injected as an Authorization header only, so it never lands in the request
body and never becomes part of the airlock payload hash or the signed receipt.

Thirty-two commands: nineteen read-only operations, thirteen airlock-gated
writes, and four governed AI decision-support commands. Billing behavior is
kept separate from AI: AI can recommend review priorities but cannot charge,
send, or alter Stripe.

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
import datetime as _datetime
import time
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

    Station v0.48+ resolves both legacy keys.local.json and the default named
    credential saved through Studio's Configure card. We retain the historical
    field aliases so existing installations need no credential migration.
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
            "no Stripe key in the vault. Open Studio → Integrations → Stripe and "
            "save a secret key with Configure. Existing legacy STRIPE_SECRET_KEY "
            "entries are also supported."
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


def _email(inputs, field="email"):
    """Return one canonical lookup/create email without changing its meaning."""
    value = _need_str(inputs, field).lower()
    if len(value) > 254 or value.count("@") != 1 or any(ch.isspace() for ch in value):
        raise RuntimeError(field + " must be a valid email address")
    local, domain = value.split("@", 1)
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise RuntimeError(field + " must be a valid email address")
    return value


def _currency(inputs, field="currency", default="usd"):
    value = str(inputs.get(field) or default).strip().lower()
    if len(value) != 3 or not value.isalpha():
        raise RuntimeError(field + " must be a three-letter alphabetic code")
    return value


def _billing_run_id(inputs, required=False):
    value = inputs.get("billing_run_id")
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise RuntimeError("billing_run_id is required for workflow-correlated billing")
        return ""
    if not isinstance(value, str):
        raise RuntimeError("billing_run_id must be a string")
    value = value.strip()
    if len(value) > 120 or not value[0].isalnum() or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
        for ch in value
    ):
        raise RuntimeError("billing_run_id has an invalid safe metadata shape")
    return value


def _metadata_fields(inputs, billing_run_id=""):
    """Validate caller metadata and add correlation without overwriting it."""
    metadata = inputs.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or len(metadata) > 50:
        raise RuntimeError("metadata must be an object with at most 50 entries")
    fields = {}
    for key, value in metadata.items():
        if not isinstance(key, str) or not key.strip() or len(key.strip()) > 40:
            raise RuntimeError("metadata keys must be non-empty strings up to 40 characters")
        key = key.strip()
        lowered = key.lower()
        if any(word in lowered for word in ("secret", "password", "token", "api_key", "private_key")):
            raise RuntimeError("metadata keys must not contain secret material")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise RuntimeError("metadata values must be scalar strings or numbers")
        text = str(value)
        if len(text) > 500 or any(ord(ch) < 32 for ch in text):
            raise RuntimeError("metadata values must be printable and at most 500 characters")
        fields["metadata[" + key + "]"] = text
    if billing_run_id:
        existing = metadata.get("billing_run_id")
        if existing is not None and str(existing) != billing_run_id:
            raise RuntimeError("metadata.billing_run_id cannot conflict with billing_run_id")
        fields.setdefault("metadata[billing_run_id]", billing_run_id)
    return fields


def _bill_client_failure(stage, states, exc):
    """Keep an ambiguous provider outcome visible in the command error."""
    states[stage]["state"] = "unknown"
    snapshot = {key: dict(value) for key, value in states.items()}
    raise RuntimeError(
        "bill_client has an unresolved provider outcome; do not recreate blindly. "
        + _json.dumps({"stages": snapshot, "error": str(exc)}, sort_keys=True)
    )


def _whole_int(value, field, minimum=None):
    """Accept integer values (or legacy integer strings), never truncate floats.

    Money arrives in cents.  ``int(12.5)`` silently becoming 12 is unsafe, so
    every cents field uses this strict parser rather than Python's coercion.
    """
    if isinstance(value, bool):
        raise RuntimeError(field + " must be a whole number")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text or (text[0] in "+-" and not text[1:].isdigit()) or (
            text[0] not in "+-" and not text.isdigit()
        ):
            raise RuntimeError(field + " must be a whole number")
        number = int(text)
    else:
        raise RuntimeError(field + " must be a whole number")
    if minimum is not None and number < minimum:
        raise RuntimeError(field + " must be at least " + str(minimum))
    return number


def _number(value, field):
    """Parse a finite numeric field without treating booleans as numbers."""
    if isinstance(value, bool):
        raise RuntimeError(field + " must be a number")
    try:
        number = float(value) if isinstance(value, (int, float, str)) else None
    except (TypeError, ValueError):
        number = None
    if number is None or number != number or number in (float("inf"), float("-inf")):
        raise RuntimeError(field + " must be a number")
    return number


def _clamp_limit(inputs, default=10):
    raw = inputs.get("limit")
    if raw is None:
        return default
    limit = _whole_int(raw, "limit")
    return max(1, min(limit, 100))


def _incremental_since(inputs):
    """Validate Station's injected timestamp and return canonical UTC + epoch."""
    raw = inputs.get("since")
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not raw.strip() or len(raw.strip()) > 64:
        raise RuntimeError("since must be a non-empty ISO-8601 timestamp string")
    text = raw.strip()
    try:
        parsed = _datetime.datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except (TypeError, ValueError):
        raise RuntimeError("since must be a valid ISO-8601 timestamp")
    if parsed.tzinfo is None:
        raise RuntimeError("since must include a timezone")
    parsed = parsed.astimezone(_datetime.timezone.utc).replace(microsecond=0)
    return parsed.strftime("%Y-%m-%dT%H:%M:%SZ"), int(parsed.timestamp())


def _incremental_seen(inputs, incremental):
    raw = inputs.get("exclude_invoice_ids")
    if raw is None:
        return set()
    if not incremental:
        raise RuntimeError("exclude_invoice_ids requires since")
    if not isinstance(raw, list):
        raise RuntimeError("exclude_invoice_ids must be an array of invoice ID strings")
    if len(raw) > 5000:
        raise RuntimeError("exclude_invoice_ids supports at most 5000 entries")
    seen = set()
    for value in raw:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 255:
            raise RuntimeError("exclude_invoice_ids entries must be non-empty strings up to 255 characters")
        seen.add(value.strip())
    return seen


def _created_at(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError("Stripe invoice response is missing a valid created timestamp")
    return _datetime.datetime.fromtimestamp(
        value, tz=_datetime.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    email = _email(inputs)

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
    limit = _clamp_limit(inputs)
    since, since_epoch = _incremental_since(inputs)
    excluded = _incremental_seen(inputs, since is not None)
    form = {"limit": limit}

    if since_epoch is not None:
        # The Station deliberately injects a lookback-adjusted watermark. GTE
        # preserves that overlap; exclude_invoice_ids removes already-delivered
        # provider IDs before any downstream effect can see them.
        form["created[gte]"] = since_epoch

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
    if not isinstance(rows, list):
        raise RuntimeError("Stripe invoice list returned an invalid data array")

    outstanding = 0
    invoices = []
    skipped_already_delivered = 0
    for row in rows:
        if not isinstance(row, dict):
            raise RuntimeError("Stripe invoice list returned a non-object invoice")
        invoice_id = row.get("id")
        if not isinstance(invoice_id, str) or not invoice_id.strip():
            raise RuntimeError("Stripe invoice response is missing a stable invoice id")
        invoice_id = invoice_id.strip()
        if invoice_id in excluded:
            skipped_already_delivered += 1
            continue
        created = row.get("created")
        created_at = _created_at(created)
        if since_epoch is not None and created < since_epoch:
            # Defensive provider-boundary check: never trust a remote filter to
            # satisfy the incremental contract on its own.
            continue
        try:
            due = _whole_int(row.get("amount_due") or 0, "Stripe amount_due", 0)
        except RuntimeError:
            raise RuntimeError("Stripe invoice response contains an invalid amount_due")
        currency = row.get("currency") or "usd"
        if row.get("status") == "open":
            outstanding += due
        invoices.append({
            "invoice_id": invoice_id,
            "number": row.get("number") or "",
            "description": row.get("description") or "",
            "status": row.get("status"),
            "amount_due": _money(due, currency),
            "amount_due_cents": due,
            "customer_id": row.get("customer"),
            "customer_email": row.get("customer_email") or "",
            "due_date": row.get("due_date"),
            "hosted_invoice_url": row.get("hosted_invoice_url") or "",
            "created": created,
            "created_at": created_at,
        })

    if since is not None:
        # Stripe lists newest-first. Scheduled delivery is oldest-first with a
        # provider-ID tie-breaker, making downstream order deterministic.
        invoices.sort(key=lambda item: (item["created_at"], item["invoice_id"]))

    currency = rows[0].get("currency") if rows else "usd"
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(invoices),
        "total_outstanding": _money(outstanding, currency),
        "total_outstanding_cents": outstanding,
        "invoices": invoices,
        "since": since or "",
        "skipped_already_delivered": skipped_already_delivered,
        "truncated": bool(parsed.get("has_more")),
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
    email = _email(inputs)

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

    currency = _currency(inputs)

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
            amount = _whole_int(item.get("amount_cents"), where + ".amount_cents")
        except RuntimeError:
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
                quantity = _whole_int(raw_quantity, where + ".quantity", 1)
            except RuntimeError:
                raise RuntimeError(where + ".quantity must be a whole number")
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
            days = _whole_int(raw_due, "days_until_due", 0)
        except RuntimeError:
            raise RuntimeError("days_until_due must be a whole number of days")
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
    """Find/create, invoice, attach one item, finalize and send.

    Each stage is reported separately. If a provider call is ambiguous, the
    exception carries the last-known stage snapshot and explicitly marks the
    uncertain stage ``unknown``; callers must reconcile by provider id before
    retrying. Idempotency scopes remain bound to the approved payload.
    """
    email = _email(inputs)
    description = _need_str(inputs, "description")
    try:
        amount = _whole_int(inputs.get("amount_cents"), "amount_cents", 1)
    except RuntimeError:
        raise RuntimeError(
            "amount_cents must be a positive whole number of cents. 250.00 dollars is 25000."
        )
    currency = _currency(inputs)
    billing_run_id = _billing_run_id(inputs)
    metadata = _metadata_fields(inputs, billing_run_id)
    raw_due = inputs.get("days_until_due")
    try:
        days = 30 if raw_due is None else _whole_int(raw_due, "days_until_due", 0)
    except RuntimeError:
        raise RuntimeError("days_until_due must be a non-negative whole number of days")
    states = {
        "customer": {"state": "unknown", "customer_id": ""},
        "invoice": {"state": "unknown", "invoice_id": ""},
        "line_items": {"state": "unknown", "attached_count": 0, "expected_count": 1},
        "finalized": {"state": "unknown", "invoice_id": ""},
        "sent": {"state": "unknown", "invoice_id": ""},
    }

    customer_created = False
    supplied_customer_id = inputs.get("customer_id")
    if supplied_customer_id is not None:
        customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")
        if not customer_id.startswith("cus_"):
            raise RuntimeError("customer_id must be a Stripe cus_ id")
        states["customer"] = {"state": "existing", "customer_id": customer_id}
    else:
        try:
            _, found = _call("GET", "/customers", {"email": email, "limit": 5})
        except Exception as exc:
            _bill_client_failure("customer", states, exc)
        rows = found.get("data") or []
        customer_id = ""
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"].strip():
                customer_id = row["id"].strip()
                break
        if customer_id:
            states["customer"] = {"state": "existing", "customer_id": customer_id}
        else:
            form = {"email": email}
            name = inputs.get("name")
            if isinstance(name, str) and name.strip():
                form["name"] = name.strip()
            form.update(metadata)
            try:
                _, parsed = _call(
                    "POST", "/customers", form,
                    idem_for="stripe.billing.bill_client.customer", inputs=inputs, stamp=stamp,
                )
            except Exception as exc:
                _bill_client_failure("customer", states, exc)
            customer_id = parsed.get("id") or ""
            if not isinstance(customer_id, str) or not customer_id.strip():
                _bill_client_failure("customer", states, RuntimeError("Stripe returned no customer id"))
            customer_id = customer_id.strip()
            customer_created = True
            states["customer"] = {"state": "created", "customer_id": customer_id}

    invoice_form = {
        "customer": customer_id,
        "collection_method": "send_invoice",
        "days_until_due": str(days),
        "description": description,
    }
    invoice_form.update(metadata)
    try:
        _, invoice = _call(
            "POST", "/invoices", invoice_form,
            idem_for="stripe.billing.bill_client.invoice", inputs=inputs, stamp=stamp,
        )
    except Exception as exc:
        _bill_client_failure("invoice", states, exc)
    invoice_id = invoice.get("id") or ""
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        _bill_client_failure("invoice", states, RuntimeError("Stripe returned no invoice id"))
    invoice_id = invoice_id.strip()
    states["invoice"] = {"state": "created", "invoice_id": invoice_id}

    try:
        _, item = _call(
            "POST", "/invoiceitems",
            {
                "customer": customer_id,
                "invoice": invoice_id,
                "description": description,
                "amount": str(amount),
                "currency": currency,
                **metadata,
            },
            idem_for="stripe.billing.bill_client.item", inputs=inputs, stamp=stamp,
        )
    except Exception as exc:
        _bill_client_failure("line_items", states, exc)
    item_id = item.get("id") if isinstance(item, dict) else ""
    if not isinstance(item_id, str) or not item_id.strip():
        _bill_client_failure("line_items", states, RuntimeError("Stripe returned no invoice item id"))
    states["line_items"] = {"state": "attached", "attached_count": 1, "expected_count": 1,
                             "line_item_id": item_id.strip()}

    try:
        status, sent = _call(
            "POST", "/invoices/" + invoice_id + "/send", {},
            idem_for="stripe.billing.bill_client.send", inputs=inputs, stamp=stamp,
        )
    except Exception as exc:
        states["sent"] = {"state": "unknown", "invoice_id": invoice_id}
        states["finalized"] = {"state": "unknown", "invoice_id": invoice_id}
        _bill_client_failure("sent", states, exc)
    sent_id = sent.get("id") or invoice_id
    if not isinstance(sent_id, str) or not sent_id.strip():
        _bill_client_failure("sent", states, RuntimeError("Stripe returned no sent invoice id"))
    sent_id = sent_id.strip()
    states["finalized"] = {"state": True, "invoice_id": sent_id}
    states["sent"] = {"state": True, "invoice_id": sent_id}
    sent_currency = sent.get("currency") or currency
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "billing_run_id": billing_run_id,
        "email_normalized": email,
        "customer_id": customer_id,
        "customer_created": customer_created,
        "invoice_id": sent_id,
        "status_at_stripe": sent.get("status"),
        "amount_due": _money(sent.get("amount_due") or 0, sent_currency),
        "sent_to": sent.get("customer_email") or email,
        "hosted_invoice_url": sent.get("hosted_invoice_url") or "",
        "dashboard_url": _dashboard("invoices", invoice_id, _api_key()) if invoice_id else "",
        "stages": states,
        "correlation": {"billing_run_id": billing_run_id, "invoice_id": sent_id,
                        "customer_id": customer_id, "line_item_count": 1},
    }, None


def stripe_billing_invoice_preview(inputs, stamp):
    """Compute a local invoice plan; this command never calls Stripe."""
    description = inputs.get("description")
    if not isinstance(description, str) or not description.strip():
        description = "Invoice preview"
    else:
        description = description.strip()
    currency = _currency(inputs)
    billing_run_id = _billing_run_id(inputs)
    _metadata_fields(inputs, billing_run_id)
    raw_items = inputs.get("line_items")
    if raw_items is None:
        raw_items = [{"description": description, "amount_cents": inputs.get("amount_cents"), "quantity": 1}]
    if not isinstance(raw_items, list) or not raw_items:
        raise RuntimeError("line_items must be a non-empty array")
    cleaned = []
    total = 0
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            raise RuntimeError("line_items[" + str(index) + "] must be an object")
        label = _need_str(item, "description")
        amount = _whole_int(item.get("amount_cents"), "line_items[" + str(index) + "].amount_cents", 1)
        quantity = _whole_int(item.get("quantity", 1), "line_items[" + str(index) + "].quantity", 1)
        line_total = amount * quantity
        total += line_total
        cleaned.append({"description": label, "amount_cents": amount,
                        "quantity": quantity, "line_total_cents": line_total})
    raw_due = inputs.get("days_until_due")
    days = 30 if raw_due is None else _whole_int(raw_due, "days_until_due", 0)
    customer_id = inputs.get("customer_id")
    if customer_id is not None and (not isinstance(customer_id, str) or not customer_id.strip()):
        raise RuntimeError("customer_id must be a non-empty string when supplied")
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "dry_run": True,
        "preview_only": True,
        "external_effect": False,
        "billing_run_id": billing_run_id,
        "customer_id": customer_id.strip() if isinstance(customer_id, str) else "",
        "email_normalized": _email(inputs) if inputs.get("email") is not None else "",
        "description": description,
        "currency": currency,
        "days_until_due": days,
        "line_items": cleaned,
        "line_item_count": len(cleaned),
        "subtotal_cents": total,
        "total_cents": total,
        "amount_due_cents": total,
        "note": "Local preview only; Stripe customer, invoice, finalization, and delivery were not touched.",
    }, None


def _invoice_status_row(row, now):
    if not isinstance(row, dict):
        raise RuntimeError("Stripe invoice response contains a non-object")
    invoice_id = row.get("id")
    if not isinstance(invoice_id, str) or not invoice_id.strip():
        raise RuntimeError("Stripe invoice response is missing a stable invoice id")
    due = _whole_int(row.get("amount_due") or 0, "Stripe amount_due", 0)
    paid = _whole_int(row.get("amount_paid") or 0, "Stripe amount_paid", 0)
    remaining = _whole_int(row.get("amount_remaining") if row.get("amount_remaining") is not None else max(0, due - paid), "Stripe amount_remaining", 0)
    due_date = row.get("due_date")
    overdue = bool(row.get("status") == "open" and isinstance(due_date, (int, float)) and due_date < now)
    days_overdue = int((now - due_date) // 86400) if overdue else 0
    if row.get("status") == "paid":
        normalized = "paid"
    elif remaining > 0 and paid > 0:
        normalized = "partially_paid"
    elif overdue:
        normalized = "overdue"
    else:
        normalized = str(row.get("status") or "unknown")
    return {
        "invoice_id": invoice_id.strip(),
        "status": normalized,
        "stripe_status": row.get("status") or "unknown",
        "currency": row.get("currency") or "usd",
        "amount_due_cents": due,
        "amount_paid_cents": paid,
        "amount_remaining_cents": remaining,
        "due_date": due_date,
        "overdue": overdue,
        "days_overdue": days_overdue,
    }


def stripe_billing_payment_status_summary(inputs, stamp):
    """Read invoice payment states and normalize overdue/partial exposure."""
    invoice_id = inputs.get("invoice_id")
    customer_id = inputs.get("customer_id")
    if invoice_id is not None:
        invoice_id = _need_str(inputs, "invoice_id")
    elif customer_id is not None:
        customer_id = _need_str(inputs, "customer_id")
    else:
        raise RuntimeError("invoice_id or customer_id is required")
    now = time.time()  # noqa: F821 -- pre-injected per the module loader
    if invoice_id:
        status, row = _call("GET", "/invoices/" + invoice_id)
        rows = [row]
    else:
        status, parsed = _call("GET", "/invoices", {"customer": customer_id, "limit": _clamp_limit(inputs, 100)})
        rows = parsed.get("data") or []
    invoices = [_invoice_status_row(row, now) for row in rows]
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "customer_id": customer_id or "",
        "invoice_id": invoice_id or "",
        "invoice_count": len(invoices),
        "invoices": invoices,
        "open_amount_remaining_cents": sum(x["amount_remaining_cents"] for x in invoices if x["stripe_status"] == "open"),
        "overdue_count": len([x for x in invoices if x["overdue"]]),
        "partial_count": len([x for x in invoices if x["status"] == "partially_paid"]),
    }, None


def stripe_billing_customer_balance_summary(inputs, stamp):
    """Read provider balance plus invoice/subscription exposure for one customer."""
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")
    status, customer = _call("GET", "/customers/" + customer_id)
    _, invoices = _call("GET", "/invoices", {"customer": customer_id, "limit": 100})
    _, subscriptions = _call("GET", "/subscriptions", {"customer": customer_id, "status": "all", "limit": 100})
    rows = invoices.get("data") or []
    now = time.time()  # noqa: F821 -- pre-injected per the module loader
    states = [_invoice_status_row(row, now) for row in rows]
    currency = (rows[0].get("currency") if rows else "usd")
    outstanding = sum(x["amount_remaining_cents"] for x in states if x["stripe_status"] in ("open", "uncollectible"))
    overdue = sum(x["amount_remaining_cents"] for x in states if x["overdue"])
    lifetime_paid = sum(x["amount_paid_cents"] for x in states if x["stripe_status"] == "paid")
    provider_balance = _whole_int(customer.get("balance") or 0, "Stripe customer balance")
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "customer_id": customer.get("id") or customer_id,
        "email": customer.get("email") or "",
        "currency": currency,
        "provider_balance_cents": provider_balance,
        "provider_balance": _money(provider_balance, currency),
        "open_or_uncollectible_remaining_cents": outstanding,
        "overdue_remaining_cents": overdue,
        "lifetime_paid_cents": lifetime_paid,
        "invoice_count": len(states),
        "active_subscription_count": len([x for x in subscriptions.get("data") or [] if x.get("status") in ("active", "trialing", "past_due")]),
        "note": "Provider balance and invoice exposure are read separately; Stripe aggregation and pagination limits apply.",
    }, None


def stripe_billing_credit_note_create(inputs, stamp):
    """Issue a credit note against a finalized invoice.

    void erases an invoice that should never have existed; a refund reverses
    money that already moved. Neither reduces what is owed on a still-live
    invoice after the fact, which is what a credit note is for.
    """
    invoice_id = _need_str(inputs, "invoice_id", "Stripe invoice ids start with in_")
    try:
        amount = _whole_int(inputs.get("amount_cents"), "amount_cents")
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
            amt = _whole_int(amount_cents, "amount_cents")
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
            months = _whole_int(inputs.get("duration_in_months"), "duration_in_months", 1)
        except RuntimeError:
            raise RuntimeError("duration_in_months is required and must be a whole number when duration is repeating")
        form["duration_in_months"] = str(months)

    if percent_off is not None:
        pct = _number(percent_off, "percent_off")
        if pct <= 0:
            raise RuntimeError("percent_off must be greater than zero")
        if pct > 100:
            raise RuntimeError("percent_off must be at most 100")
        form["percent_off"] = str(pct)
        currency = "usd"
    else:
        try:
            cents = _whole_int(amount_off, "amount_cents_off")
        except Exception:
            raise RuntimeError("amount_cents_off must be a whole number of cents")
        if cents <= 0:
            raise RuntimeError("amount_cents_off must be greater than zero")
        currency = _currency(inputs)
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
            n = _whole_int(max_redemptions, "max_redemptions", 1)
        except RuntimeError:
            raise RuntimeError("max_redemptions must be a whole number")
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


# ------------------------------------------------------------- v1.2 additions

def stripe_billing_product_create(inputs, stamp):
    """Create a product. Foundation for price_create."""
    name = _need_str(inputs, "name")
    form = {"name": name}
    description = inputs.get("description")
    if isinstance(description, str) and description.strip():
        form["description"] = description.strip()

    status, parsed = _call(
        "POST", "/products", form,
        idem_for="stripe.billing.product_create", inputs=inputs, stamp=stamp,
    )
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "product_id": parsed.get("id") or "",
        "name": parsed.get("name") or "",
        "active": bool(parsed.get("active")),
    }, None


def stripe_billing_product_list(inputs, stamp):
    status, parsed = _call("GET", "/products", {"limit": _clamp_limit(inputs)})
    rows = parsed.get("data") or []
    products = [
        {"product_id": row.get("id"), "name": row.get("name") or "", "active": bool(row.get("active"))}
        for row in rows
    ]
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(products),
        "products": products,
    }, None


def stripe_billing_price_create(inputs, stamp):
    """Create a price: one-time, recurring, or metered.

    No `interval` -> one-time. `interval` alone -> standard recurring.
    `meter_id` -> metered, and Stripe requires interval alongside it, since a
    metered price is always billed on a schedule even though the quantity
    comes from usage rather than a flat unit count.
    """
    product_id = _need_str(inputs, "product_id", "run product_create or product_list to get one")
    try:
        amount = _whole_int(inputs.get("unit_amount_cents"), "unit_amount_cents")
    except Exception:
        raise RuntimeError("unit_amount_cents must be a whole number of cents")
    if amount <= 0:
        raise RuntimeError("unit_amount_cents must be greater than zero")
    currency = _currency(inputs)

    interval = inputs.get("interval")
    if isinstance(interval, str) and interval.strip():
        interval = interval.strip().lower()
        if interval not in ("day", "week", "month", "year"):
            raise RuntimeError("interval must be one of: day, week, month, year")
    else:
        interval = None

    meter_id = inputs.get("meter_id")
    if isinstance(meter_id, str) and meter_id.strip():
        meter_id = meter_id.strip()
        if not interval:
            raise RuntimeError("meter_id requires interval too: a metered price is still billed on a schedule")
    else:
        meter_id = None

    form = {"product": product_id, "currency": currency, "unit_amount": str(amount)}
    if meter_id:
        form["recurring[usage_type]"] = "metered"
        form["recurring[meter]"] = meter_id
        form["recurring[interval]"] = interval
    elif interval:
        form["recurring[interval]"] = interval

    status, parsed = _call(
        "POST", "/prices", form,
        idem_for="stripe.billing.price_create", inputs=inputs, stamp=stamp,
    )
    recurring = parsed.get("recurring") or {}
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "price_id": parsed.get("id") or "",
        "product_id": parsed.get("product") or product_id,
        "type": parsed.get("type"),
        "unit_amount": _money(parsed.get("unit_amount") or amount, parsed.get("currency") or currency),
        "interval": recurring.get("interval") or "",
        "metered": bool(recurring.get("meter")),
    }, None


def stripe_billing_price_list(inputs, stamp):
    form = {"limit": _clamp_limit(inputs)}
    product_id = inputs.get("product_id")
    if isinstance(product_id, str) and product_id.strip():
        form["product"] = product_id.strip()

    status, parsed = _call("GET", "/prices", form)
    rows = parsed.get("data") or []
    prices = []
    for row in rows:
        recurring = row.get("recurring") or {}
        prices.append({
            "price_id": row.get("id"),
            "product_id": row.get("product"),
            "type": row.get("type"),
            "unit_amount": _money(row.get("unit_amount") or 0, row.get("currency") or "usd"),
            "interval": recurring.get("interval") or "",
            "metered": bool(recurring.get("meter")),
        })
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(prices),
        "prices": prices,
    }, None


def stripe_billing_usage_record_create(inputs, stamp):
    """Record usage against a metered price via a Billing Meter event.

    Stripe has two coexisting APIs for this: the legacy per-subscription-item
    usage_records endpoint, and the newer Billing Meters event API. This
    module uses Billing Meters, the path Stripe steers new integrations
    toward, confirmed live rather than assumed.

    Takes meter_id rather than the meter's event_name, and looks the
    event_name up, so the operator only ever has to know the meter id they
    already have from price_create.
    """
    meter_id = _need_str(inputs, "meter_id", "the meter id used when the metered price was created")
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")

    raw_value = inputs.get("value")
    if raw_value is None:
        raise RuntimeError("value is required, and may be 0 to record zero usage")
    try:
        value = _whole_int(raw_value, "value", 0)
    except RuntimeError:
        raise RuntimeError("value must be a non-negative whole number")

    _, meter = _call("GET", "/billing/meters/" + meter_id)
    event_name = meter.get("event_name")
    if not event_name:
        raise RuntimeError("Stripe has no such meter: " + meter_id)

    form = {
        "event_name": event_name,
        "payload[stripe_customer_id]": customer_id,
        "payload[value]": str(value),
    }
    status, parsed = _call(
        "POST", "/billing/meter_events", form,
        idem_for="stripe.billing.usage_record_create", inputs=inputs, stamp=stamp,
    )
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "event_id": parsed.get("identifier") or "",
        "meter_id": meter_id,
        "customer_id": customer_id,
        "value": value,
        "note": "aggregation is asynchronous; usage_summary_list may take up to "
                "about 30 seconds to reflect this event",
    }, None


def stripe_billing_usage_summary_list(inputs, stamp):
    """Read aggregated usage for a meter and customer over a time window.

    Verified live: a summary is not immediately available after
    usage_record_create returns. Stripe aggregates asynchronously, roughly a
    20 second lag observed in testing. An empty result right after recording
    usage does not mean the event was lost.
    """
    meter_id = _need_str(inputs, "meter_id", "the meter id used when the metered price was created")
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")

    end_time = inputs.get("end_time")
    start_time = inputs.get("start_time")
    now = int(time.time())  # noqa: F821 -- pre-injected per the module loader
    end_val = _whole_int(end_time, "end_time", 0) if end_time is not None else now
    start_val = _whole_int(start_time, "start_time", 0) if start_time is not None else end_val - 86400
    if start_val >= end_val:
        raise RuntimeError("start_time must be earlier than end_time")

    status, parsed = _call(
        "GET", "/billing/meters/" + meter_id + "/event_summaries",
        {"customer": customer_id, "start_time": start_val, "end_time": end_val},
    )
    rows = parsed.get("data") or []
    summaries = [
        {"start_time": row.get("start_time"), "end_time": row.get("end_time"),
         "aggregated_value": row.get("aggregated_value")}
        for row in rows
    ]
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "found": len(summaries),
        "summaries": summaries,
        "note": "aggregation lags roughly 20-30 seconds behind usage_record_create",
    }, None


def stripe_billing_mandate_get(inputs, stamp):
    """Retrieve a mandate. Mandates cannot be created directly through the
    API, verified live (POST /v1/mandates does not exist) -- they only exist
    as a side effect of confirming a SetupIntent or PaymentIntent with a
    payment method type that needs future-debit authorization (SEPA, ACH,
    BACS, etc). Listing mandates requires a Stripe preview API version, not
    used here since it is unstable and inconsistent with the rest of this
    module. Retrieval by id is the one stable, documented operation.
    """
    mandate_id = _need_str(inputs, "mandate_id", "Stripe mandate ids start with mandate_")
    status, parsed = _call("GET", "/mandates/" + mandate_id)
    acceptance = parsed.get("customer_acceptance") or {}
    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "http_status": status,
        "mandate_id": parsed.get("id") or mandate_id,
        "status_at_stripe": parsed.get("status"),
        "type": parsed.get("type"),
        "payment_method_id": parsed.get("payment_method") or "",
        "acceptance_type": acceptance.get("type") or "",
        "accepted_at": acceptance.get("accepted_at"),
    }, None


# ------------------------------------------------------------------ LLM helpers

def _llm_complete(messages, step_id="legacy_ai_command"):
    """Call Station's governed LLM path for decision support only."""
    try:
        import station_llm as _sl  # noqa: F401 -- injected by station ROOT path
        result = _sl.complete(
            messages,
            provider="groq",
            module_id="dave/stripe-invoicing",
            module_version="v1.4.0",
            step_id=step_id,
            caller_kind="module",
        )
    except ImportError:
        raise RuntimeError(
            "station.llm is not available on this station version. "
            "Update to station v0.45 or later to use AI commands."
        )
    error = result.get("error")
    if error:
        raise RuntimeError(f"LLM call failed: {error}")
    reply = result.get("reply") or ""
    if not reply:
        raise RuntimeError("LLM returned an empty reply. Check your Groq API key in the vault.")
    return reply, result.get("receipt_id", "")


def _legacy_invoice_description_generate(inputs, stamp):
    """Generate a professional invoice line-item description using an LLM.

    Takes a short context (service name, period, client name) and returns
    a polished, ready-to-use description string suitable for invoice_create
    or bill_client. The LLM call is governed: a signed egress receipt is
    written to the Receipts tab under kind=egress.

    Requires a Groq API key stored in the vault:
        keys.local.json: {"groq": {"GROQ_API_KEY": "gsk_..."}}

    Returns:
        description: the generated line-item description
        receipt_id:  the egress receipt id (eg/...) for audit
    """
    service = _need_str(inputs, "service", "e.g. 'Monthly retainer', 'API integration work'")
    period = str(inputs.get("period") or "").strip()
    client = str(inputs.get("client_name") or "").strip()
    tone = str(inputs.get("tone") or "professional").strip().lower()
    if tone not in ("professional", "friendly", "brief"):
        raise RuntimeError("tone must be one of: professional, friendly, brief")

    context_parts = [f"Service: {service}"]
    if period:
        context_parts.append(f"Period: {period}")
    if client:
        context_parts.append(f"Client: {client}")

    system_prompt = (
        "You are a billing assistant. Generate a concise, professional invoice "
        "line-item description. Output only the description text — no quotes, "
        "no explanation, no prefix. Maximum 120 characters."
    )
    user_prompt = (
        f"Generate a {tone} invoice line-item description for:\n"
        + "\n".join(context_parts)
    )

    reply, receipt_id = _llm_complete([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "description": reply.strip(),
        "receipt_id": receipt_id,
        "note": "LLM call governed via station.llm — egress receipt written to audit log",
    }, None


def stripe_billing_dunning_message_draft(inputs, stamp):
    """Draft a payment reminder from minimum non-identifying facts only."""
    amount_due_cents = _whole_int(inputs.get("amount_due_cents"), "amount_due_cents", 1)
    days_overdue = _whole_int(inputs.get("days_overdue"), "days_overdue", 0)
    tone = str(inputs.get("tone") or "polite").strip().lower()
    sender_name = str(inputs.get("sender_name") or "Billing Team").strip()
    currency = _currency(inputs)

    if tone not in ("polite", "firm", "urgent"):
        raise RuntimeError("tone must be one of: polite, firm, urgent")
    if not sender_name or len(sender_name) > 80:
        raise RuntimeError("sender_name must be a non-empty string up to 80 characters")
    if "@" in sender_name or sender_name.startswith(("in_", "cus_")):
        raise RuntimeError("sender_name must be a non-identifying sender label, not an email or Stripe id")
    if len(currency) != 3 or not currency.isalpha():
        raise RuntimeError("currency must be a three-letter alphabetic code")

    facts = {
        "amount_due_cents": amount_due_cents,
        "currency": currency,
        "days_overdue": days_overdue,
        "tone": tone,
        "sender_label": sender_name,
    }

    system_prompt = (
        "You are a billing assistant. Draft a human-reviewed payment reminder "
        "from only the non-identifying facts supplied. Do not infer a customer "
        "identity, invoice identifier, email address, legal threat, or payment "
        "promise. Return strict JSON with exactly two string fields: subject "
        "(maximum 160 characters) and body (maximum 2000 characters)."
    )
    user_prompt = (
        "Minimised overdue-payment facts with no customer, contact, account, "
        "or Stripe identifiers: " + _json.dumps(facts, sort_keys=True, separators=(",", ":"))
    )

    reply, receipt_id = _llm_complete([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ], "dunning_message_draft")

    result = _decision_json(reply, "dunning_message_draft")
    if set(result) != {"subject", "body"}:
        _invalid_ai_response("dunning_message_draft", "must contain exactly subject and body")
    subject = _ai_string("dunning_message_draft", result, "subject", maximum=160)
    body = _ai_string("dunning_message_draft", result, "body", maximum=2000)

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "subject": subject,
        "body": body,
        "receipt_id": receipt_id,
        "decision_support_only": True,
        "draft_only": True,
        "note": "Draft only — review before sending. This command cannot send, charge, or alter Stripe.",
    }, None


def _legacy_client_summary_insight(inputs, stamp):
    """Generate a plain-English account insight from customer summary data.

    Takes the output of customer_summary (or equivalent fields) and produces
    a concise paragraph suitable for account reviews, CRM notes, or client
    communications. The LLM call is governed: a signed egress receipt is
    written to the Receipts tab under kind=egress.

    Requires a Groq API key stored in the vault:
        keys.local.json: {"groq": {"GROQ_API_KEY": "gsk_..."}}

    Returns:
        insight:    plain-English summary paragraph
        receipt_id: egress receipt id for audit
    """
    customer_id = _need_str(inputs, "customer_id", "Stripe customer ids start with cus_")
    customer_email = str(inputs.get("customer_email") or "").strip()
    open_invoices = inputs.get("open_invoices_count")
    paid_invoices = inputs.get("paid_invoices_count")
    lifetime_paid = str(inputs.get("lifetime_paid") or "").strip()
    active_subscriptions = inputs.get("active_subscriptions_count")
    focus = str(inputs.get("focus") or "general").strip().lower()

    if focus not in ("general", "risk", "opportunity", "payment_health"):
        raise RuntimeError("focus must be one of: general, risk, opportunity, payment_health")

    context_parts = [f"Customer ID: {customer_id}"]
    if customer_email:
        context_parts.append(f"Email: {customer_email}")
    if open_invoices is not None:
        context_parts.append(f"Open invoices: {open_invoices}")
    if paid_invoices is not None:
        context_parts.append(f"Paid invoices: {paid_invoices}")
    if lifetime_paid:
        context_parts.append(f"Lifetime paid: {lifetime_paid}")
    if active_subscriptions is not None:
        context_parts.append(f"Active subscriptions: {active_subscriptions}")

    system_prompt = (
        "You are a billing analyst. Produce a concise 2-3 sentence account insight "
        "from the data provided. Be factual and specific. No preamble. "
        "Output only the insight paragraph."
    )
    user_prompt = (
        f"Generate a {focus} account insight:\n"
        + "\n".join(context_parts)
    )

    reply, receipt_id = _llm_complete([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ])

    return {
        "ok": True,
        "loaded_from": "module:dave/stripe-invoicing",
        "customer_id": customer_id,
        "insight": reply.strip(),
        "receipt_id": receipt_id,
        "note": "LLM call governed via station.llm — egress receipt written to audit log",
    }, None


# ---------------------------------------------------- AI decision support

def _metric(inputs, field, required=True, minimum=0):
    raw = inputs.get(field)
    if raw is None and not required:
        return 0
    try:
        value = _whole_int(raw, field)
    except RuntimeError:
        raise RuntimeError(field + " must be a whole number")
    if value < minimum:
        raise RuntimeError(field + " must be at least " + str(minimum))
    return value


def _decision_json(reply, command_id):
    """Accept structured model output only; never promote free text as advice."""
    text = reply.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3].rstrip()
    try:
        parsed = _json.loads(text)
    except Exception:
        raise RuntimeError("invalid_ai_response: " + command_id + " returned non-JSON decision support")
    if not isinstance(parsed, dict):
        raise RuntimeError("invalid_ai_response: " + command_id + " returned a non-object decision payload")
    return parsed


def _invalid_ai_response(command_id, detail):
    raise RuntimeError("invalid_ai_response: " + command_id + " " + detail)


def _ai_string(command_id, result, field, allowed=None, maximum=600):
    value = result.get(field)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        _invalid_ai_response(command_id, field + " must be a non-empty string up to " + str(maximum) + " characters")
    value = value.strip()
    if allowed is not None and value not in allowed:
        _invalid_ai_response(command_id, field + " must be one of: " + ", ".join(allowed))
    return value


def _ai_int(command_id, result, field, minimum, maximum):
    value = result.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        _invalid_ai_response(command_id, field + " must be an integer from " + str(minimum) + " to " + str(maximum))
    return value


def _validate_risk_response(result):
    command_id = "payment_risk_assess"
    clean = {
        "risk_level": _ai_string(command_id, result, "risk_level", ("low", "medium", "high"), 10),
        "risk_score": _ai_int(command_id, result, "risk_score", 0, 100),
        "recommended_review": _ai_string(command_id, result, "recommended_review", maximum=400),
    }
    drivers = result.get("drivers")
    if not isinstance(drivers, list) or not 1 <= len(drivers) <= 3:
        _invalid_ai_response(command_id, "drivers must be an array with 1 to 3 items")
    clean["drivers"] = []
    for driver in drivers:
        if not isinstance(driver, str) or not driver.strip() or len(driver.strip()) > 200:
            _invalid_ai_response(command_id, "drivers entries must be non-empty strings up to 200 characters")
        clean["drivers"].append(driver.strip())
    return clean


def _validate_strategy_response(result):
    command_id = "collection_strategy_recommend"
    return {
        "urgency": _ai_string(command_id, result, "urgency", ("low", "medium", "high"), 10),
        "recommended_action": _ai_string(command_id, result, "recommended_action", maximum=300),
        "wait_days": _ai_int(command_id, result, "wait_days", 0, 30),
        "escalation_needed": _ai_string(command_id, result, "escalation_needed", ("yes", "no"), 3),
        "rationale": _ai_string(command_id, result, "rationale", maximum=600),
    }


def _validate_anomaly_response(result, permitted_refs):
    command_id = "billing_anomaly_detect"
    clean = {
        "portfolio_risk": _ai_string(command_id, result, "portfolio_risk", ("low", "medium", "high"), 10),
        "anomalies": [],
        "recommended_review_order": [],
    }
    anomalies = result.get("anomalies")
    if not isinstance(anomalies, list) or len(anomalies) > len(permitted_refs):
        _invalid_ai_response(command_id, "anomalies must be an array no larger than the submitted portfolio")
    seen = set()
    for anomaly in anomalies:
        if not isinstance(anomaly, dict):
            _invalid_ai_response(command_id, "each anomaly must be an object")
        ref = anomaly.get("record_ref")
        if not isinstance(ref, str) or ref not in permitted_refs or ref in seen:
            _invalid_ai_response(command_id, "anomaly record_ref must be a unique submitted opaque reference")
        seen.add(ref)
        severity = anomaly.get("severity")
        reason = anomaly.get("reason")
        if severity not in ("low", "medium", "high"):
            _invalid_ai_response(command_id, "anomaly severity must be low, medium, or high")
        if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 300:
            _invalid_ai_response(command_id, "anomaly reason must be a non-empty string up to 300 characters")
        clean["anomalies"].append({"record_ref": ref, "severity": severity, "reason": reason.strip()})
    review_order = result.get("recommended_review_order")
    if not isinstance(review_order, list) or len(review_order) > len(permitted_refs):
        _invalid_ai_response(command_id, "recommended_review_order must be an array within the submitted portfolio")
    for ref in review_order:
        if not isinstance(ref, str) or ref not in permitted_refs or ref in clean["recommended_review_order"]:
            _invalid_ai_response(command_id, "recommended_review_order entries must be unique submitted opaque references")
        clean["recommended_review_order"].append(ref)
    return clean


def _decision_result(result, receipt_id):
    result["ok"] = True
    result["loaded_from"] = "module:dave/stripe-invoicing"
    result["receipt_id"] = receipt_id
    result["decision_support_only"] = True
    result["note"] = "Review before acting; this command cannot send, charge, or alter Stripe."
    return result, None


def stripe_billing_payment_risk_assess(inputs, stamp):
    """Assess payment risk from aggregate metrics only; no PII is sent to the LLM."""
    metrics = {
        "open_invoice_count": _metric(inputs, "open_invoice_count"),
        "overdue_invoice_count": _metric(inputs, "overdue_invoice_count"),
        "oldest_overdue_days": _metric(inputs, "oldest_overdue_days"),
        "outstanding_cents": _metric(inputs, "outstanding_cents"),
        "paid_on_time_count": _metric(inputs, "paid_on_time_count", required=False),
        "paid_late_count": _metric(inputs, "paid_late_count", required=False),
    }
    reply, receipt_id = _llm_complete([
        {"role": "system", "content": "You are a conservative accounts-receivable risk analyst. Assess only the aggregate metrics supplied. Do not infer identity, invent facts, or recommend an automated action. Return strict JSON with risk_level (low|medium|high), risk_score (0-100 integer), drivers (max 3 strings), and recommended_review (string)."},
        {"role": "user", "content": "Aggregate account metrics, with no customer identity or contact data: " + _json.dumps(metrics, sort_keys=True, separators=(",", ":"))},
    ], "payment_risk_assess")
    return _decision_result(_validate_risk_response(_decision_json(reply, "payment_risk_assess")), receipt_id)


def stripe_billing_collection_strategy_recommend(inputs, stamp):
    """Recommend a reviewable collection step from minimised delinquency facts."""
    risk_level = _need_str(inputs, "risk_level").lower()
    if risk_level not in ("low", "medium", "high"):
        raise RuntimeError("risk_level must be one of: low, medium, high")
    facts = {
        "risk_level": risk_level,
        "days_overdue": _metric(inputs, "days_overdue"),
        "outstanding_cents": _metric(inputs, "outstanding_cents"),
        "prior_reminder_count": _metric(inputs, "prior_reminder_count"),
        "dispute_open": _metric(inputs, "dispute_open", required=False),
        "payment_commitment_present": _metric(inputs, "payment_commitment_present", required=False),
    }
    messages = [
        {"role": "system", "content": (
            "You are a conservative collections operations advisor. Recommend "
            "a human-reviewed next step only; never draft or send a message, "
            "never promise legal action, and never decide automatically. "
            "OUTPUT CONTRACT: respond with exactly one JSON object and no other "
            "text, markdown, code fence, or commentary. The object must contain "
            "exactly these fields: urgency (one of low, medium, high), "
            "recommended_action (string), wait_days (integer 0 through 30), "
            "escalation_needed (yes or no), and rationale (string, at most two "
            "sentences). Begin with { and end with }."
        )},
        {"role": "user", "content": (
            "Minimised collections facts with no customer, invoice, email, or "
            "account identifiers. Return only the JSON object described above: "
            + _json.dumps(facts, sort_keys=True, separators=(",", ":"))
        )},
    ]
    reply, receipt_id = _llm_complete(messages, "collection_strategy_recommend")
    try:
        parsed = _decision_json(reply, "collection_strategy_recommend")
    except RuntimeError as first_error:
        # groq_chat prepends Studio's general assistant prompt and does not
        # expose a native response_format option. A single governed correction
        # call makes the JSON contract reliable without accepting prose or
        # weakening the validator. Any second failure remains fail-closed.
        retry_messages = [
            {"role": "system", "content": (
                "The previous response was rejected because it was not a JSON "
                "object. Retry now. Return ONLY one compact JSON object with "
                "exactly: urgency, recommended_action, wait_days, "
                "escalation_needed, rationale. No prose, markdown, or code fences."
            )},
            messages[1],
        ]
        retry_reply, retry_receipt_id = _llm_complete(
            retry_messages, "collection_strategy_recommend_retry"
        )
        try:
            parsed = _decision_json(retry_reply, "collection_strategy_recommend")
        except RuntimeError:
            raise first_error
        receipt_id = retry_receipt_id
    return _decision_result(_validate_strategy_response(parsed), receipt_id)


def stripe_billing_billing_anomaly_detect(inputs, stamp):
    """Review anonymised portfolio metrics for billing anomalies before action."""
    period = _need_str(inputs, "billing_period")
    if len(period) > 32:
        raise RuntimeError("billing_period must be 32 characters or fewer")
    raw_portfolio = inputs.get("portfolio")
    if not isinstance(raw_portfolio, list) or not raw_portfolio:
        raise RuntimeError("portfolio must be a non-empty array of anonymised metrics")
    if len(raw_portfolio) > 50:
        raise RuntimeError("portfolio supports at most 50 records per assessment")
    portfolio = []
    for row in raw_portfolio:
        if not isinstance(row, dict):
            raise RuntimeError("each portfolio record must be an object")
        ref = _need_str(row, "record_ref", "use an opaque local reference, never an email or Stripe customer id")
        if "@" in ref or ref.startswith(("cus_", "in_")) or len(ref) > 64:
            raise RuntimeError("record_ref must be an opaque non-PII reference, not an email or Stripe id")
        amount = _metric(row, "amount_cents")
        prior = _metric(row, "prior_amount_cents", required=False)
        portfolio.append({
            "record_ref": ref,
            "amount_cents": amount,
            "prior_amount_cents": prior,
            "invoice_count": _metric(row, "invoice_count", required=False),
            "overdue_days": _metric(row, "overdue_days", required=False),
            "change_cents": amount - prior,
        })
    payload = {
        "billing_period": period,
        "portfolio_baseline_cents": _metric(inputs, "portfolio_baseline_cents", required=False),
        "records": portfolio,
    }
    reply, receipt_id = _llm_complete([
        {"role": "system", "content": "You are a conservative billing-control analyst. Identify only review candidates from anonymised portfolio metrics. Do not infer identity, accuse fraud, or authorize charges. Return strict JSON with portfolio_risk (low|medium|high), anomalies (array of objects containing record_ref, severity low|medium|high, reason), and recommended_review_order (array of record_ref values)."},
        {"role": "user", "content": "Anonymised billing portfolio; record_ref values are opaque tokens and no contact data is present: " + _json.dumps(payload, sort_keys=True, separators=(",", ":"))},
    ], "billing_anomaly_detect")
    return _decision_result(
        _validate_anomaly_response(_decision_json(reply, "billing_anomaly_detect"), {row["record_ref"] for row in portfolio}),
        receipt_id,
    )
