"""Real SMS (Africa's Talking, faked here), per-store PINs, sales and payouts."""
import re
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

from app import app, connect, init_db, seed_db
from payments.sms import SmsResult, parse_response
from seed_menus import seed_menus

app_module = sys.modules["app"]
KEYS = ("PAYMENT_PROVIDER", "SMS_PROVIDER", "ALLOW_SHARED_PIN", "PRODUCTION",
        "STORE_COMMISSION_PERCENT", "DATABASE", "CASHIER_PIN")


class FakeSms:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send(self, to, message):
        self.sent.append((to, message))
        if self.fail:
            from payments.sms import SmsError
            raise SmsError("network down")
        return SmsResult("SENT", provider_id="ATXid_1", cost="UGX 30")


class Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = str(Path(self.temp_dir.name) / "test.db")
        self.old = {k: app.config.get(k) for k in KEYS}
        app.config.update(TESTING=True, DATABASE=self.database, PAYMENT_PROVIDER="demo",
                          SMS_PROVIDER="simulated", ALLOW_SHARED_PIN=True, PRODUCTION=False,
                          STORE_COMMISSION_PERCENT=5.0, CASHIER_PIN="2026")
        init_db(self.database)
        seed_db(self.database)
        seed_menus(self.database)
        self.client = app.test_client()
        self.old_client = app_module.sms_client

    def tearDown(self):
        app.config.update(self.old)
        app_module.sms_client = self.old_client
        self.temp_dir.cleanup()

    def csrf(self, client, url):
        return re.search(r'name="csrf_token" value="([^"]+)"', client.get(url).get_data(as_text=True)).group(1)

    def db(self):
        return connect(self.database)

    def buy(self):
        db = self.db()
        item = db.execute("SELECT id, merchant_id FROM menu_items WHERE is_active=1 LIMIT 1").fetchone()
        db.close()
        url = f"/buy/menu/{item['id']}"
        r = self.client.post(url, data={"csrf_token": self.csrf(self.client, url), "sender_name": "Amina",
                                        "sender_phone": "0750111222", "recipient_phone": "0770123456",
                                        "payment_method": "mtn"})
        return r.location.rsplit("/", 1)[-1], item["merchant_id"]

    def login(self, client, merchant_id, pin):
        token = self.csrf(client, "/merchant/verify")
        return client.post("/merchant/login", data={"csrf_token": token, "merchant_id": merchant_id, "pin": pin},
                           follow_redirects=True).get_data(as_text=True)


class SmsTests(Base):
    def test_parse_africastalking_replies(self):
        ok = parse_response({"SMSMessageData": {"Recipients": [
            {"statusCode": 101, "status": "Success", "messageId": "ATXid_9", "cost": "UGX 30"}]}})
        self.assertEqual((ok.status, ok.provider_id), ("SENT", "ATXid_9"))
        bad = parse_response({"SMSMessageData": {"Recipients": [{"statusCode": 403, "status": "InvalidPhoneNumber"}]}})
        self.assertEqual((bad.status, bad.error), ("FAILED", "InvalidPhoneNumber"))
        self.assertEqual(parse_response({}).status, "FAILED")

    def test_paid_gift_texts_recipient_and_sender(self):
        code, _ = self.buy()
        rows = self.db().execute("SELECT * FROM sms_outbox ORDER BY id").fetchall()
        self.assertEqual([r["kind"] for r in rows], ["GIFT", "RECEIPT"])
        self.assertEqual(rows[0]["to_phone"], "+256770123456")
        self.assertIn(code, rows[0]["body"])
        self.assertIn("Show code", rows[0]["body"])
        self.assertEqual(rows[1]["to_phone"], "+256750111222")
        self.assertTrue(all(r["status"] == "SIMULATED" for r in rows))

    def test_gift_text_has_link_only_for_a_public_address(self):
        old = app.config["PUBLIC_BASE_URL"]
        try:
            app.config["PUBLIC_BASE_URL"] = "https://smartgifting.onrender.com"
            code, _ = self.buy()
            body = self.db().execute("SELECT body FROM sms_outbox WHERE kind='GIFT'").fetchone()[0]
            self.assertIn(f"https://smartgifting.onrender.com/voucher/{code}", body)
            app.config["PUBLIC_BASE_URL"] = "http://127.0.0.1:5000"
            self.buy()
            body = self.db().execute("SELECT body FROM sms_outbox WHERE kind='GIFT' ORDER BY id DESC").fetchone()[0]
            self.assertNotIn("http", body)
        finally:
            app.config["PUBLIC_BASE_URL"] = old

    def test_real_provider_used_when_configured(self):
        fake = FakeSms()
        app_module.sms_client = lambda: fake
        app.config["SMS_PROVIDER"] = "africastalking"
        self.buy()
        self.assertEqual(len(fake.sent), 2)
        row = self.db().execute("SELECT * FROM sms_outbox WHERE kind='GIFT'").fetchone()
        self.assertEqual((row["status"], row["provider_id"]), ("SENT", "ATXid_1"))

    def test_sms_failure_does_not_lose_the_paid_gift(self):
        app_module.sms_client = lambda: FakeSms(fail=True)
        app.config["SMS_PROVIDER"] = "africastalking"
        code, _ = self.buy()
        db = self.db()
        self.assertEqual(db.execute("SELECT status FROM vouchers WHERE voucher_code=?", (code,)).fetchone()[0], "ACTIVE")
        self.assertEqual(db.execute("SELECT status FROM sms_outbox WHERE kind='GIFT'").fetchone()[0], "FAILED")

    def test_pickup_code_is_never_stored_in_the_outbox(self):
        fake = FakeSms()
        app_module.sms_client = lambda: fake
        app.config["SMS_PROVIDER"] = "africastalking"
        code, merchant_id = self.buy()
        db = self.db()
        db.execute("UPDATE vouchers SET face_lock=1 WHERE voucher_code=?", (code,))
        db.commit()
        cashier = app.test_client()
        self.login(cashier, merchant_id, "2026")
        token = self.csrf(cashier, f"/merchant/verify?code={code}")
        page = cashier.post("/merchant/pickup-code/send", data={"csrf_token": token, "voucher_code": code},
                            follow_redirects=True).get_data(as_text=True)
        sent_code = re.search(r"pickup code for .* is (\d{6})", fake.sent[-1][1]).group(1)
        self.assertNotIn(sent_code, page)            # the cashier does not see it
        stored = self.db().execute("SELECT body FROM sms_outbox WHERE kind='PICKUP_CODE'").fetchone()[0]
        self.assertNotIn(sent_code, stored)

    def test_outbox_page_hidden_when_hosted(self):
        self.assertEqual(self.client.get("/dev/sms").status_code, 200)
        app.config["PRODUCTION"] = True
        self.assertEqual(self.client.get("/dev/sms").status_code, 404)


class StorePinTests(Base):
    def store(self):
        return self.db().execute("SELECT id FROM merchants ORDER BY id LIMIT 1").fetchone()[0]

    def set_pin(self, merchant_id, pin):
        db = self.db()
        db.execute("UPDATE merchants SET cashier_pin_hash=? WHERE id=?", (generate_password_hash(pin), merchant_id))
        db.commit()

    def test_own_pin_replaces_shared_pin(self):
        m = self.store()
        self.set_pin(m, "482913")
        self.assertIn("Store or PIN is incorrect", self.login(app.test_client(), m, "2026"))
        self.assertIn("Signed in to", self.login(app.test_client(), m, "482913"))

    def test_pin_from_one_store_does_not_open_another(self):
        ids = [r[0] for r in self.db().execute("SELECT id FROM merchants ORDER BY id LIMIT 2")]
        self.set_pin(ids[0], "482913")
        self.set_pin(ids[1], "771100")
        self.assertIn("Store or PIN is incorrect", self.login(app.test_client(), ids[1], "482913"))

    def test_hosted_site_refuses_stores_without_their_own_pin(self):
        app.config["ALLOW_SHARED_PIN"] = False
        self.assertIn("has no PIN yet", self.login(app.test_client(), self.store(), "2026"))

    def test_lockout_after_repeated_wrong_pins(self):
        m = self.store()
        self.set_pin(m, "482913")
        client = app.test_client()
        for _ in range(5):
            self.login(client, m, "000000")
        self.assertIn("Too many wrong PINs", self.login(client, m, "482913"))

    def test_cli_sets_pin(self):
        runner = app.test_cli_runner()
        name = self.db().execute("SELECT name FROM merchants ORDER BY id LIMIT 1").fetchone()[0]
        self.assertIn("6 to 12 digits", runner.invoke(args=["set-store-pin", name, "--pin", "12"]).output)
        out = runner.invoke(args=["set-store-pin", name.upper(), "--pin", "482913"]).output
        self.assertIn("PIN set", out)
        self.assertIn("Signed in to", self.login(app.test_client(), self.store(), "482913"))


class SalesTests(Base):
    def test_balance_counts_collected_gifts_less_fee_and_payouts(self):
        code, merchant_id = self.buy()
        self.buy()                                   # a second gift, still waiting
        db = self.db()
        value = db.execute("SELECT price_at_purchase FROM vouchers WHERE voucher_code=?", (code,)).fetchone()[0]
        db.execute("UPDATE vouchers SET status='USED', redeemed_at=? WHERE voucher_code=?",
                   ("2026-11-26 10:00:00", code))
        db.commit()
        name = db.execute("SELECT name FROM merchants WHERE id=?", (merchant_id,)).fetchone()[0]
        app.test_cli_runner().invoke(args=["record-payout", name, "1000", "--reference", "MP1"])
        with app.app_context():
            sales = app_module.store_sales(app_module.get_db(), merchant_id)
        self.assertEqual((sales["sold_n"], sales["collected_n"], sales["waiting_n"]), (2, 1, 1))
        self.assertEqual(sales["earned"], round(value * 0.95))
        self.assertEqual(sales["balance"], round(value * 0.95) - 1000)

    def test_sales_page_needs_sign_in_and_shows_own_store(self):
        self.assertIn("/merchant/verify", self.client.get("/merchant/sales").location)
        _, merchant_id = self.buy()
        cashier = app.test_client()
        self.login(cashier, merchant_id, "2026")
        page = cashier.get("/merchant/sales").get_data(as_text=True)
        self.assertIn("Smart Gifting owes you", page)
        self.assertIn("Waiting", page)


class StorePinsSettingTests(Base):
    def test_store_pins_setting_sets_hashes(self):
        name = self.db().execute("SELECT name FROM merchants ORDER BY id LIMIT 1").fetchone()[0]
        n = app_module.apply_store_pins(self.database, f" {name.lower()} : 482913 ; Nowhere:111111; Bad:12")
        self.assertEqual(n, 1)
        row = self.db().execute("SELECT cashier_pin_hash FROM merchants WHERE name=?", (name,)).fetchone()[0]
        self.assertTrue(row and "482913" not in row)
        self.assertEqual(app_module.apply_store_pins(self.database, f"{name}:482913"), 0)  # unchanged


if __name__ == "__main__":
    unittest.main()
