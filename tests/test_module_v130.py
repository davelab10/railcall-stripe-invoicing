import importlib.util
import json
import pathlib
import sys
import unittest
from unittest import mock


MODULE_DIR = pathlib.Path(__file__).resolve().parents[1]
MANIFEST_PATH = MODULE_DIR / "module.json"
HANDLER_PATH = MODULE_DIR / "handlers" / "handler.py"
STATION_WORKBENCH = pathlib.Path.home() / ".railcall" / "station" / "workbench"

OLD_COMMAND_IDS = {
    "stripe.billing.customer_find",
    "stripe.billing.invoice_list",
    "stripe.billing.invoice_get",
    "stripe.billing.subscription_list",
    "stripe.billing.customer_create",
    "stripe.billing.invoice_create",
    "stripe.billing.invoice_send",
    "stripe.billing.invoice_void",
    "stripe.billing.subscription_cancel",
    "stripe.billing.customer_summary",
    "stripe.billing.aging_report",
    "stripe.billing.bill_client",
    "stripe.billing.credit_note_create",
    "stripe.billing.refund_create",
    "stripe.billing.payment_method_list",
    "stripe.billing.coupon_list",
    "stripe.billing.coupon_create",
    "stripe.billing.promotion_code_create",
    "stripe.billing.product_create",
    "stripe.billing.product_list",
    "stripe.billing.price_create",
    "stripe.billing.price_list",
    "stripe.billing.usage_record_create",
    "stripe.billing.usage_summary_list",
    "stripe.billing.mandate_get",
    "stripe.billing.payment_risk_assess",
    "stripe.billing.collection_strategy_recommend",
    "stripe.billing.billing_anomaly_detect",
}


def _load_handler():
    spec = importlib.util.spec_from_file_location("stripe_invoicing_handler", HANDLER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HANDLER = _load_handler()


def _invoice(invoice_id, created, *, status="open", amount_due=1250):
    return {
        "id": invoice_id,
        "created": created,
        "number": "INV-" + invoice_id[-2:],
        "status": status,
        "amount_due": amount_due,
        "currency": "usd",
        "customer": "cus_fixture",
        "customer_email": "fixture@example.test",
        "due_date": created + 86400,
        "hosted_invoice_url": "https://example.test/invoice/" + invoice_id,
    }


class ManifestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.commands = cls.manifest["commands"]
        cls.by_id = {command["id"]: command for command in cls.commands}

    def test_version_count_and_old_command_regression(self):
        self.assertEqual(self.manifest["version"], "1.3.0")
        self.assertEqual(len(self.commands), 29)
        self.assertEqual(len(self.by_id), 29)
        self.assertTrue(OLD_COMMAND_IDS.issubset(self.by_id))
        self.assertIn("stripe.billing.dunning_message_draft", self.by_id)

    def test_all_commands_resolve_and_require_receipts(self):
        missing = []
        for command in self.commands:
            function_name = command["id"].replace(".", "_").replace("-", "_")
            if not callable(getattr(HANDLER, function_name, None)):
                missing.append((command["id"], function_name))
            self.assertIs(command.get("receipt_required"), True, command["id"])
        self.assertEqual(missing, [])

    def test_incremental_contract_parses_with_station_v065(self):
        self.assertTrue(STATION_WORKBENCH.is_dir(), "active Station workbench not found")
        sys.path.insert(0, str(STATION_WORKBENCH))
        try:
            from primitives import incremental_contract

            parsed = incremental_contract.parse(self.by_id["stripe.billing.invoice_list"])
        finally:
            sys.path.pop(0)
        self.assertEqual(parsed["incremental"]["since_param"], "since")
        self.assertEqual(parsed["incremental"]["seen_param"], "exclude_invoice_ids")
        self.assertEqual(parsed["incremental"]["items_field"], "invoices")
        self.assertEqual(parsed["incremental"]["cursor_field"], "invoice_id")
        self.assertEqual(parsed["incremental"]["watermark_from"], "created_at")
        self.assertEqual(parsed["incremental"]["cursor_stability"], "provider_id")
        self.assertEqual(parsed["schedulable"], {
            "min_interval_minutes": 15,
            "max_runtime_minutes": 5,
            "concurrency": "skip",
        })

    def test_station_v065_semantic_firewall_accepts_supported_number_types(self):
        self.assertTrue(STATION_WORKBENCH.is_dir(), "active Station workbench not found")
        sys.path.insert(0, str(STATION_WORKBENCH))
        try:
            import approval_airlock

            invoice_list = self.by_id["stripe.billing.invoice_list"]
            dunning = self.by_id["stripe.billing.dunning_message_draft"]
            self.assertEqual(invoice_list["input_schema"]["limit"]["type"], "number")
            self.assertEqual(dunning["input_schema"]["amount_due_cents"]["type"], "number")
            self.assertEqual(dunning["input_schema"]["days_overdue"]["type"], "number")
            self.assertNotIn(
                "integer",
                {
                    invoice_list["input_schema"]["limit"]["type"],
                    dunning["input_schema"]["amount_due_cents"]["type"],
                    dunning["input_schema"]["days_overdue"]["type"],
                },
            )
            invoice_ok, invoice_errors = approval_airlock.validate(
                invoice_list, {"limit": 10}
            )
            dunning_ok, dunning_errors = approval_airlock.validate(
                dunning, {"amount_due_cents": 1000, "days_overdue": 30}
            )
        finally:
            sys.path.pop(0)
        self.assertTrue(invoice_ok, invoice_errors)
        self.assertTrue(dunning_ok, dunning_errors)

    def test_dunning_is_read_only_and_does_not_accept_identifiers(self):
        command = self.by_id["stripe.billing.dunning_message_draft"]
        self.assertEqual(command["mode"], "read")
        self.assertEqual(command["side_effects"], "none")
        self.assertEqual(command["provider"], "groq")
        fields = set(command["input_schema"])
        self.assertNotIn("customer_email", fields)
        self.assertNotIn("invoice_id", fields)
        self.assertNotIn("customer_id", fields)

    def test_capabilities_not_widened(self):
        self.assertEqual(
            self.manifest["requires"],
            {
                "network": ["api.stripe.com", "api.groq.com"],
                "subprocess": False,
                "filesystem_writes": [],
            },
        )


class InvoiceListTests(unittest.TestCase):
    def _run(self, inputs, rows, *, has_more=False):
        captured = {}

        def fake_call(method, path, form=None, **kwargs):
            captured.update({"method": method, "path": path, "form": form})
            return 200, {"data": rows, "has_more": has_more}

        with mock.patch.object(HANDLER, "_call", side_effect=fake_call):
            result, error = HANDLER.stripe_billing_invoice_list(inputs, "stamp")
        self.assertIsNone(error)
        return result, captured

    def test_normal_manual_behavior_remains_compatible(self):
        result, captured = self._run(
            {"customer_id": "cus_123", "status": "open", "limit": "10"},
            [_invoice("in_new", 1722470400)],
        )
        self.assertEqual(captured["form"], {"limit": 10, "customer": "cus_123", "status": "open"})
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["invoices"][0]["invoice_id"], "in_new")
        self.assertIn("customer_email", result["invoices"][0])
        self.assertIn("hosted_invoice_url", result["invoices"][0])
        self.assertFalse(result["truncated"])

    def test_incremental_filters_seen_and_orders_deterministically(self):
        since = "2024-08-01T00:00:00Z"
        rows = [
            _invoice("in_b", 1722470520),
            _invoice("in_seen", 1722470460),
            _invoice("in_c", 1722470520),
            _invoice("in_a", 1722470460),
        ]
        result, captured = self._run(
            {"since": since, "exclude_invoice_ids": ["in_seen"], "limit": 25},
            rows,
        )
        self.assertEqual(captured["form"]["created[gte]"], 1722470400)
        self.assertEqual(
            [item["invoice_id"] for item in result["invoices"]],
            ["in_a", "in_b", "in_c"],
        )
        self.assertEqual(result["skipped_already_delivered"], 1)
        self.assertEqual(result["since"], since)
        for item in result["invoices"]:
            self.assertRegex(item["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertTrue(item["invoice_id"].startswith("in_"))

    def test_complete_and_capped_truncation(self):
        complete, _ = self._run(
            {"since": "2024-08-01T00:00:00Z"},
            [_invoice("in_a", 1722470460)],
            has_more=False,
        )
        capped, _ = self._run(
            {"since": "2024-08-01T00:00:00Z", "limit": 1},
            [_invoice("in_a", 1722470460)],
            has_more=True,
        )
        self.assertFalse(complete["truncated"])
        self.assertTrue(capped["truncated"])

    def test_malformed_incremental_inputs_fail_clearly(self):
        invalid = [
            {"since": "not-a-date"},
            {"since": "2024-08-01T00:00:00"},
            {"since": "2024-08-01T00:00:00Z", "exclude_invoice_ids": "in_a"},
            {"exclude_invoice_ids": ["in_a"]},
            {"limit": 10.5},
            {"limit": 1.5},
        ]
        for inputs in invalid:
            with self.subTest(inputs=inputs):
                with self.assertRaises(RuntimeError):
                    HANDLER.stripe_billing_invoice_list(inputs, "stamp")

    def test_missing_cursor_or_watermark_fails(self):
        for row in (
            {**_invoice("in_a", 1722470460), "id": ""},
            {**_invoice("in_a", 1722470460), "created": None},
        ):
            with self.subTest(row=row):
                with self.assertRaises(RuntimeError):
                    self._run({"since": "2024-08-01T00:00:00Z"}, [row])


class ExistingSafetyRegressionTests(unittest.TestCase):
    def test_whole_cent_validation_still_rejects_bool_and_float(self):
        for value in (True, False, 12.5, "12.5"):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    HANDLER._whole_int(value, "amount_cents", 1)

    def test_write_idempotency_still_uses_approved_payload_hash(self):
        captured = {}

        def payload_hash(scope, inputs):
            captured["scope"] = scope
            captured["inputs"] = inputs
            return "idem-approved-payload"

        def post_form(url, form, timeout, headers):
            captured["url"] = url
            captured["headers"] = headers
            return 200, b'{"id":"cus_fixture"}'

        helpers = {
            "vault_get": lambda provider: {"STRIPE_SECRET_KEY": "sk_test_fixture"},
            "airlock_payload_hash": payload_hash,
            "http_post_form": post_form,
        }
        with mock.patch.object(HANDLER, "__rc_helpers__", helpers, create=True):
            status, result = HANDLER._call(
                "POST",
                "/customers",
                {"email": "fixture@example.test"},
                idem_for="stripe.billing.customer_create",
                inputs={"email": "fixture@example.test"},
            )
        self.assertEqual(status, 200)
        self.assertEqual(result["id"], "cus_fixture")
        self.assertEqual(captured["headers"]["Idempotency-Key"], "idem-approved-payload")
        self.assertEqual(captured["scope"], "stripe.billing.customer_create")

    def test_missing_idempotency_helper_fails_before_write(self):
        post_form = mock.Mock()
        helpers = {
            "vault_get": lambda provider: {"STRIPE_SECRET_KEY": "sk_test_fixture"},
            "http_post_form": post_form,
        }
        with mock.patch.object(HANDLER, "__rc_helpers__", helpers, create=True):
            with self.assertRaisesRegex(RuntimeError, "Stripe idempotency requires"):
                HANDLER._call(
                    "POST",
                    "/customers",
                    {"email": "fixture@example.test"},
                    idem_for="stripe.billing.customer_create",
                    inputs={"email": "fixture@example.test"},
                )
        post_form.assert_not_called()


class DunningDraftTests(unittest.TestCase):
    def _run_with_reply(self, inputs, reply, receipt_id="eg/test"):
        captured = {}

        def fake_complete(messages, step_id=""):
            captured["messages"] = messages
            captured["step_id"] = step_id
            return reply, receipt_id

        with mock.patch.object(HANDLER, "_llm_complete", side_effect=fake_complete), mock.patch.object(
            HANDLER, "_call", side_effect=AssertionError("dunning draft must not call Stripe")
        ):
            result, error = HANDLER.stripe_billing_dunning_message_draft(inputs, "stamp")
        self.assertIsNone(error)
        return result, captured

    def test_valid_json_is_structured_draft_and_preserves_receipt(self):
        result, captured = self._run_with_reply(
            {
                "amount_due_cents": 12500,
                "days_overdue": 7,
                "tone": "firm",
                "sender_name": "Accounts Receivable",
                "customer_email": "private@example.test",
                "invoice_id": "in_private",
            },
            '{"subject":"Payment reminder","body":"Please review the overdue balance."}',
        )
        prompt = json.dumps(captured["messages"])
        self.assertNotIn("private@example.test", prompt)
        self.assertNotIn("in_private", prompt)
        self.assertEqual(captured["step_id"], "dunning_message_draft")
        self.assertEqual(result["subject"], "Payment reminder")
        self.assertEqual(result["receipt_id"], "eg/test")
        self.assertTrue(result["decision_support_only"])
        self.assertTrue(result["draft_only"])

    def test_malformed_or_wrong_shape_ai_output_fails_closed(self):
        inputs = {"amount_due_cents": 12500, "days_overdue": 7}
        replies = [
            "plain text",
            "[]",
            '{"subject":"Only subject"}',
            '{"subject":"ok","body":"ok","send":true}',
            '{"subject":"","body":"ok"}',
            json.dumps({"subject": "x" * 161, "body": "ok"}),
            json.dumps({"subject": "ok", "body": "x" * 2001}),
        ]
        for reply in replies:
            with self.subTest(reply=reply[:40]):
                with self.assertRaisesRegex(RuntimeError, "invalid_ai_response"):
                    self._run_with_reply(inputs, reply)

    def test_invalid_inputs_fail_before_llm(self):
        invalid = [
            {"amount_due_cents": 0, "days_overdue": 1},
            {"amount_due_cents": 100, "days_overdue": -1},
            {"amount_due_cents": 1.5, "days_overdue": 1},
            {"amount_due_cents": 1000.5, "days_overdue": 30},
            {"amount_due_cents": 1000, "days_overdue": 30.5},
            {"amount_due_cents": 100, "days_overdue": 1, "tone": "hostile"},
            {"amount_due_cents": 100, "days_overdue": 1, "currency": "US"},
            {"amount_due_cents": 100, "days_overdue": 1, "sender_name": "billing@example.test"},
            {"amount_due_cents": 100, "days_overdue": 1, "sender_name": "in_private"},
        ]
        with mock.patch.object(HANDLER, "_llm_complete") as llm:
            for inputs in invalid:
                with self.subTest(inputs=inputs):
                    with self.assertRaises(RuntimeError):
                        HANDLER.stripe_billing_dunning_message_draft(inputs, "stamp")
            llm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
