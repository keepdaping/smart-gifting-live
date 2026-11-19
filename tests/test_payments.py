"""Real payments through Pesapal, tested against a fake Pesapal server."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import app, connect, init_db, seed_db
from payments.pesapal import PesapalClient
from seed_menus import seed_menus

app_module = sys.modules["app"]


class FakePesapal:
    """Answers like Pesapal API 3.0 and records every call."""

    def __init__(self):
        self.calls = []
        self.status = {}          # tracking_id -> GetTransactionStatus reply
        self.fail_submit = False
        self.orders = 0

    def __call__(self, method, url, headers, body, timeout):
        path = urlparse(url).path
        self.calls.append((method, path, body, headers))
        if path.endswith("/api/Auth/RequestToken"):
            return 200, json.dumps({"token": "tok-123", "expiryDate": "2099-01-01T00:00:00Z",
                                    "error": None, "status": "200"}).encode()
        assert headers.get("Authorization") == "Bearer tok-123"
        if path.endswith("/api/URLSetup/RegisterIPN"):
            return 200, json.dumps({"url": body["url"], "ipn_id": "ipn-abc", "error": None,
                                    "status": "200"}).encode()
        if path.endswith("/api/Transactions/SubmitOrderRequest"):
            if self.fail_submit:
                return 500, json.dumps({"error": {"code": "x", "message": "boom"}}).encode()
            self.orders += 1
            tracking = f"track-{self.orders}"
            self.status[tracking] = {"status_code": 0, "payment_status_description": "INVALID",
                                     "amount": body["amount"], "currency": body["currency"],
                                     "merchant_reference": body["id"], "payment_method": ""}
            return 200, json.dumps({"order_tracking_id": tracking, "merchant_reference": body["id"],
                                    "redirect_url": f"https://pay.example/{tracking}",
                                    "error": None, "status": "200"}).encode()
        if path.endswith("/api/Transactions/GetTransactionStatus"):
            tracking = parse_qs(urlparse(url).query)["orderTrackingId"][0]
            reply = dict(self.status[tracking], error=None, status="200")
            return 200, json.dumps(reply).encode()
        return 404, b"{}"

    def pay(self, tracking, method="MTN UG", amount=None):
        s = self.status[tracking]
        s.update(status_code=1, payment_status_description="Completed", payment_method=method,
                 confirmation_code="MP123")
        if amount is not None:
            s["amount"] = amount

    def count(self, suffix):
        return sum(1 for _, path, _, _ in self.calls if path.endswith(suffix))


class PesapalPaymentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "test.db")
        self.old_config = {k: app.config[k] for k in
                           ("PAYMENT_PROVIDER", "PESAPAL_CONSUMER_KEY", "PESAPAL_CONSUMER_SECRET",
                            "PESAPAL_ENV", "PESAPAL_IPN_ID", "PUBLIC_BASE_URL")}
        app.config.update(TESTING=True, DATABASE=self.database, PAYMENT_PROVIDER="pesapal",
                          PESAPAL_CONSUMER_KEY="key", PESAPAL_CONSUMER_SECRET="secret",
                          PESAPAL_ENV="sandbox", PESAPAL_IPN_ID="",
                          PUBLIC_BASE_URL="https://gifts.example")
        init_db(self.database)
        seed_db(self.database)
        seed_menus(self.database)
        self.fake = FakePesapal()
        app_module._pesapal.clear()
        app_module._pesapal[("key", "sandbox")] = PesapalClient("key", "secret", "sandbox",
                                                                transport=self.fake)
        self.client = app.test_client()
        db = connect(self.database)
        self.item = db.execute("SELECT id, price_ugx FROM menu_items WHERE is_active=1 "
                               "ORDER BY id LIMIT 1").fetchone()
        db.close()

    def tearDown(self):
        app.config.update(self.old_config)
        app_module._pesapal.clear()
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------------
    def buy(self, client=None):
        client = client or self.client
        url = f"/buy/menu/{self.item['id']}"
        page = client.get(url).get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        return client.post(url, data={"csrf_token": csrf, "sender_name": "Amina Nakato",
                                      "sender_phone": "0750111222", "recipient_phone": "0770123456",
                                      "message": "Enjoy", "face_lock": "1"})

    def rows(self, sql, *args):
        db = connect(self.database)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    # -- tests -------------------------------------------------------------------
    def test_checkout_offers_real_payment_not_demo(self):
        page = self.client.get(f"/buy/menu/{self.item['id']}").get_data(as_text=True)
        self.assertIn("Pesapal", page)
        self.assertIn("MTN Mobile Money, Airtel Money or a card", page)
        self.assertNotIn("Demo only", page)
        self.assertIn('name="sender_phone"', page)

    def test_buying_sends_sender_to_pesapal_and_creates_no_voucher_yet(self):
        response = self.buy()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, "https://pay.example/track-1")
        order = self.rows("SELECT * FROM orders")[0]
        self.assertEqual(order["status"], "PENDING")
        self.assertEqual(order["tracking_id"], "track-1")
        self.assertEqual(order["amount"], self.item["price_ugx"])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"], 0)

        submit = [b for _, p, b, _ in self.fake.calls if p.endswith("SubmitOrderRequest")][0]
        self.assertEqual(submit["currency"], "UGX")
        self.assertEqual(submit["amount"], self.item["price_ugx"])
        self.assertEqual(submit["id"], order["reference"])
        self.assertEqual(submit["notification_id"], "ipn-abc")
        self.assertEqual(submit["callback_url"], "https://gifts.example/payment/return")
        self.assertEqual(submit["billing_address"]["phone_number"], "256750111222")

    def test_ipn_url_is_registered_once_and_token_is_reused(self):
        self.buy()
        self.buy()
        self.assertEqual(self.fake.count("RegisterIPN"), 1)
        self.assertEqual(self.fake.count("RequestToken"), 1)
        registered = [b for _, p, b, _ in self.fake.calls if p.endswith("RegisterIPN")][0]
        self.assertEqual(registered["url"], "https://gifts.example/payment/pesapal/ipn")

    def test_paid_order_issues_exactly_one_voucher(self):
        self.buy()
        self.fake.pay("track-1")
        response = self.client.get("/payment/return?OrderTrackingId=track-1"
                                   "&OrderMerchantReference=x&OrderNotificationType=CALLBACKURL")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/voucher/", response.location)
        # Pesapal may notify several times; still one voucher.
        for _ in range(3):
            self.client.get("/payment/pesapal/ipn?OrderTrackingId=track-1&OrderNotificationType=IPNCHANGE")
        vouchers = self.rows("SELECT * FROM vouchers")
        self.assertEqual(len(vouchers), 1)
        voucher = vouchers[0]
        self.assertEqual(voucher["monetary_value"], self.item["price_ugx"])
        self.assertEqual(voucher["payment_method"], "MTN UG")
        self.assertEqual(voucher["sender_phone"], "+256750111222")
        self.assertEqual(voucher["face_lock"], 1)
        order = self.rows("SELECT * FROM orders")[0]
        self.assertEqual((order["status"], order["voucher_code"], order["confirmation_code"]),
                         ("PAID", voucher["voucher_code"], "MP123"))
        # the sender lands on the voucher with the share dialog, and cannot register their own face
        page = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("Payment received", page)
        with self.client.session_transaction() as sess:
            self.assertIn(voucher["voucher_code"], sess["sent_codes"])

    def test_ipn_alone_issues_voucher_and_replies_in_pesapal_format(self):
        self.buy()
        self.fake.pay("track-1", method="Airtel UG")
        reply = self.client.get("/payment/pesapal/ipn?OrderTrackingId=track-1"
                                "&OrderMerchantReference=SG-1&OrderNotificationType=IPNCHANGE").get_json()
        self.assertEqual(reply, {"orderNotificationType": "IPNCHANGE", "orderTrackingId": "track-1",
                                 "orderMerchantReference": "SG-1", "status": 200})
        self.assertEqual(self.rows("SELECT payment_method FROM vouchers")[0]["payment_method"], "Airtel UG")

    def test_ipn_post_needs_no_csrf_token(self):
        self.buy()
        self.fake.pay("track-1")
        response = self.client.post("/payment/pesapal/ipn", json={
            "OrderTrackingId": "track-1", "OrderMerchantReference": "SG-1",
            "OrderNotificationType": "IPNCHANGE"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], 200)
        self.assertEqual(len(self.rows("SELECT * FROM vouchers")), 1)

    def test_waiting_then_failed_payment_creates_no_voucher(self):
        self.buy()
        response = self.client.get("/payment/return?OrderTrackingId=track-1")
        self.assertIn("/payment/SG-", response.location)
        waiting = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("Approve the payment on your phone", waiting)
        self.fake.status["track-1"].update(status_code=2, payment_status_description="Failed")
        failed = self.client.get(response.location).get_data(as_text=True)
        self.assertIn("The payment didn't go through", failed)
        self.assertEqual(self.rows("SELECT status FROM orders")[0]["status"], "FAILED")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"], 0)

    def test_waiting_page_issues_voucher_once_payment_arrives(self):
        self.buy()
        reference = self.rows("SELECT reference FROM orders")[0]["reference"]
        self.fake.pay("track-1")
        response = self.client.get(f"/payment/{reference}")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/voucher/", response.location)

    def test_underpaid_or_wrong_currency_is_not_accepted(self):
        self.buy()
        self.fake.pay("track-1", amount=self.item["price_ugx"] - 1000)
        self.client.get("/payment/pesapal/ipn?OrderTrackingId=track-1")
        self.assertEqual(self.rows("SELECT status FROM orders")[0]["status"], "PENDING")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM vouchers")[0]["n"], 0)

    def test_pesapal_down_means_nothing_charged_and_form_shown_again(self):
        self.fake.fail_submit = True
        response = self.buy()
        self.assertEqual(response.status_code, 400)
        self.assertIn("Nothing was charged", response.get_data(as_text=True))
        self.assertEqual(self.rows("SELECT status FROM orders")[0]["status"], "FAILED")

    def test_unknown_tracking_id_is_ignored(self):
        reply = self.client.get("/payment/pesapal/ipn?OrderTrackingId=nope").get_json()
        self.assertEqual(reply["status"], 500)
        self.assertEqual(self.client.get("/payment/return?OrderTrackingId=nope").status_code, 404)

    def test_other_people_cannot_see_someone_elses_pending_order(self):
        self.buy()
        reference = self.rows("SELECT reference FROM orders")[0]["reference"]
        self.assertEqual(app.test_client().get(f"/payment/{reference}").status_code, 404)

    def test_return_from_another_browser_shows_voucher_without_share_rights(self):
        self.buy()
        self.fake.pay("track-1")
        other = app.test_client()
        response = other.get("/payment/return?OrderTrackingId=track-1")
        self.assertIn("/voucher/", response.location)
        with other.session_transaction() as sess:
            self.assertNotIn("sent_codes", sess)


class DemoModeTests(unittest.TestCase):
    """Without Pesapal keys the app still runs as the simulated demo."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "test.db")
        self.old = app.config["PAYMENT_PROVIDER"]
        app.config.update(TESTING=True, DATABASE=self.database, PAYMENT_PROVIDER="demo")
        init_db(self.database)
        seed_db(self.database)
        seed_menus(self.database)
        self.client = app.test_client()

    def tearDown(self):
        app.config["PAYMENT_PROVIDER"] = self.old
        self.temp_dir.cleanup()

    def test_demo_purchase_records_a_paid_order_and_voucher(self):
        db = connect(self.database)
        item = db.execute("SELECT id FROM menu_items WHERE is_active=1 LIMIT 1").fetchone()
        db.close()
        url = f"/buy/menu/{item['id']}"
        page = self.client.get(url).get_data(as_text=True)
        self.assertIn("Demo only", page)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post(url, data={"csrf_token": csrf, "sender_name": "Amina",
                                               "sender_phone": "0750111222",
                                               "recipient_phone": "0770123456",
                                               "payment_method": "airtel"})
        self.assertIn("/voucher/", response.location)
        db = connect(self.database)
        order = db.execute("SELECT * FROM orders").fetchone()
        voucher = db.execute("SELECT * FROM vouchers").fetchone()
        db.close()
        self.assertEqual((order["status"], order["provider"]), ("PAID", "demo"))
        self.assertEqual(voucher["payment_method"], "Airtel Money")
        self.assertEqual(voucher["order_id"], order["id"])

    def test_sender_phone_is_required(self):
        db = connect(self.database)
        item = db.execute("SELECT id FROM menu_items WHERE is_active=1 LIMIT 1").fetchone()
        db.close()
        url = f"/buy/menu/{item['id']}"
        page = self.client.get(url).get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        response = self.client.post(url, data={"csrf_token": csrf, "sender_name": "Amina",
                                               "recipient_phone": "0770123456",
                                               "payment_method": "mtn"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Enter your Ugandan mobile number", response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
