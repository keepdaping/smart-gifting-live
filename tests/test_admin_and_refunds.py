"""Admin panel (real stores, gifts, photos, payouts), live mode and refunds."""
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app import app, connect, init_db, seed_db
from payments.pesapal import PesapalError
from seed_menus import seed_menus

m = sys.modules["app"]
KEYS = ("PAYMENT_PROVIDER", "SMS_PROVIDER", "ADMIN_PASSWORD", "DATABASE", "ALLOW_SHARED_PIN",
        "PRODUCTION", "SEED_DEMO_STORES")


def jpeg_bytes():
    import cv2
    ok, buf = cv2.imencode(".jpg", np.full((1600, 900, 3), 120, dtype=np.uint8))
    return buf.tobytes()


class FakePesapal:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def refund(self, **kw):
        self.calls.append(kw)
        if self.fail:
            raise PesapalError("Pesapal error: refund rejected")
        return "Refund request successfully"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.database = str(Path(self.tmp.name) / "t.db")
        self.old = {k: app.config.get(k) for k in KEYS}
        app.config.update(TESTING=True, DATABASE=self.database, PAYMENT_PROVIDER="demo", SMS_PROVIDER="simulated",
                          ADMIN_PASSWORD="s3cret-admin", ALLOW_SHARED_PIN=True, PRODUCTION=False)
        init_db(self.database)
        self.client = app.test_client()
        self.old_pesapal = m.pesapal_client

    def tearDown(self):
        app.config.update(self.old)
        m.pesapal_client = self.old_pesapal
        self.tmp.cleanup()

    def csrf(self, client=None, url="/admin"):
        client = client or self.client
        return re.search(r'name="csrf_token" value="([^"]+)"', client.get(url).get_data(as_text=True)).group(1)

    def admin(self):
        self.client.post("/admin/login", data={"csrf_token": self.csrf(), "password": "s3cret-admin"})

    def db(self):
        return connect(self.database)

    def add_store(self, **extra):
        data = {"csrf_token": self.csrf(url="/admin/store/new"), "name": "Mama's Bakery", "category": "Bakery",
                "description": "Fresh cakes in Jinja", "payout_phone": "0770123456", "pin": "482913",
                "is_active": "1", **extra}
        r = self.client.post("/admin/store/new", data=data, content_type="multipart/form-data")
        return int(r.location.rstrip("/").rsplit("/", 1)[-1])

    def add_item(self, store_id, **extra):
        data = {"csrf_token": self.csrf(url=f"/admin/store/{store_id}"), "name": "Chocolate cake",
                "variant": "1 kg", "price_ugx": "45,000", "category": "Cakes", "tags": "birthday", **extra}
        return self.client.post(f"/admin/store/{store_id}/items", data=data, content_type="multipart/form-data")


class AdminTests(Base):
    def test_admin_needs_password(self):
        self.assertIn("Admin password", self.client.get("/admin").get_data(as_text=True))
        self.assertEqual(self.client.get("/admin/gifts").status_code, 302)
        r = self.client.post("/admin/login", data={"csrf_token": self.csrf(), "password": "wrong"},
                             follow_redirects=True)
        self.assertIn("Wrong password", r.get_data(as_text=True))

    def test_admin_hidden_when_no_password_set(self):
        app.config["ADMIN_PASSWORD"] = ""
        self.assertEqual(self.client.get("/admin").status_code, 404)

    def test_admin_lockout(self):
        for _ in range(5):
            self.client.post("/admin/login", data={"csrf_token": self.csrf(), "password": "x"})
        r = self.client.post("/admin/login", data={"csrf_token": self.csrf(), "password": "s3cret-admin"},
                             follow_redirects=True)
        self.assertIn("Too many wrong passwords", r.get_data(as_text=True))

    def test_add_store_with_photo_and_pin_then_sell_a_gift(self):
        self.admin()
        store_id = self.add_store(photo=(io.BytesIO(jpeg_bytes()), "shop.jpg"))
        store = self.db().execute("SELECT * FROM merchants WHERE id=?", (store_id,)).fetchone()
        self.assertEqual(store["payout_phone"], "+256770123456")
        self.assertTrue(store["cashier_pin_hash"] and "482913" not in store["cashier_pin_hash"])
        img = self.client.get(store["image_url"])
        self.assertEqual((img.status_code, img.mimetype), (200, "image/jpeg"))
        import cv2
        decoded = cv2.imdecode(np.frombuffer(img.data, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(max(decoded.shape[:2]), 1200)        # resized

        self.add_item(store_id, photo=(io.BytesIO(jpeg_bytes()), "cake.jpg"))
        item = self.db().execute("SELECT * FROM menu_items WHERE merchant_id=?", (store_id,)).fetchone()
        self.assertEqual((item["price_ugx"], item["variant"]), (45000, "1 kg"))
        self.assertTrue(item["image_filename"].startswith("db:"))
        home = self.client.get("/").get_data(as_text=True)
        self.assertIn("Mama&#39;s Bakery", home)
        page = self.client.get(f"/store/{store_id}").get_data(as_text=True)
        self.assertIn("Chocolate cake", page)
        self.assertIn(f"/img/{item['image_filename'][3:]}", page)
        self.assertEqual(self.client.get(f"/buy/menu/{item['id']}").status_code, 200)

    def test_bad_inputs_are_rejected(self):
        self.admin()
        r = self.client.post("/admin/store/new", data={"csrf_token": self.csrf(url="/admin/store/new"),
                                                       "name": "X", "category": "", "pin": "12"},
                             content_type="multipart/form-data")
        body = r.get_data(as_text=True)
        self.assertIn("Enter the store", body)
        self.assertIn("6 to 12 digits", body)
        store_id = self.add_store()
        self.add_item(store_id, price_ugx="100")
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 0)
        r = self.add_item(store_id, photo=(io.BytesIO(b"not an image"), "x.jpg"))
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM menu_items").fetchone()[0], 0)

    def test_hidden_store_disappears_from_the_site(self):
        self.admin()
        store_id = self.add_store()
        self.add_item(store_id)
        data = {"csrf_token": self.csrf(url=f"/admin/store/{store_id}"), "name": "Mama's Bakery",
                "category": "Bakery"}               # is_active unticked
        self.client.post(f"/admin/store/{store_id}", data=data, content_type="multipart/form-data")
        self.assertNotIn("Mama&#39;s Bakery", self.client.get("/").get_data(as_text=True))
        self.assertEqual(self.client.get(f"/store/{store_id}").status_code, 404)
        item = self.db().execute("SELECT id FROM menu_items").fetchone()[0]
        self.assertEqual(self.client.get(f"/buy/menu/{item}").status_code, 404)

    def test_edit_item_price_and_hide(self):
        self.admin()
        store_id = self.add_store()
        self.add_item(store_id)
        item_id = self.db().execute("SELECT id FROM menu_items").fetchone()[0]
        self.client.post(f"/admin/item/{item_id}", data={"csrf_token": self.csrf(url=f"/admin/item/{item_id}"),
                                                         "name": "Chocolate cake", "price_ugx": "50000",
                                                         "category": "Cakes"}, content_type="multipart/form-data")
        row = self.db().execute("SELECT price_ugx, is_active FROM menu_items").fetchone()
        self.assertEqual(tuple(row), (50000, 0))

    def test_record_payout(self):
        self.admin()
        store_id = self.add_store()
        self.client.post(f"/admin/store/{store_id}/payout",
                         data={"csrf_token": self.csrf(url=f"/admin/store/{store_id}"), "amount": "20,000",
                               "reference": "MP77"})
        self.assertEqual(self.db().execute("SELECT amount, reference FROM payouts").fetchone()[:], (20000, "MP77"))


class LiveModeTests(unittest.TestCase):
    def test_live_site_starts_without_demo_stores(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "live.db")
            old = app.config["SEED_DEMO_STORES"]
            app.config["SEED_DEMO_STORES"] = False
            try:
                m.bootstrap(db_path)
            finally:
                app.config["SEED_DEMO_STORES"] = old
            self.assertEqual(sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM merchants").fetchone()[0], 0)


class RefundTests(Base):
    def setUp(self):
        super().setUp()
        seed_db(self.database)
        seed_menus(self.database)

    def buy(self):
        db = self.db()
        item = db.execute("SELECT id, merchant_id FROM menu_items WHERE is_active=1 LIMIT 1").fetchone()
        url = f"/buy/menu/{item['id']}"
        buyer = app.test_client()
        buyer.post(url, data={"csrf_token": self.csrf(buyer, url), "sender_name": "Amina",
                              "sender_phone": "0750111222", "recipient_phone": "0770123456",
                              "payment_method": "mtn"})
        return self.db().execute("SELECT * FROM orders ORDER BY id DESC").fetchone(), item["merchant_id"]

    def refund(self, reference):
        self.admin()
        return self.client.post(f"/admin/gifts/{reference}/refund",
                                data={"csrf_token": self.csrf(url="/admin/gifts")}, follow_redirects=True)

    def test_refund_blocks_the_code_and_texts_the_sender(self):
        order, merchant_id = self.buy()
        self.assertIn("Refund started", self.refund(order["reference"]).get_data(as_text=True))
        db = self.db()
        self.assertEqual(db.execute("SELECT status FROM orders").fetchone()[0], "REFUNDED")
        self.assertEqual(db.execute("SELECT status FROM vouchers").fetchone()[0], "REFUNDED")
        self.assertEqual(db.execute("SELECT kind FROM sms_outbox ORDER BY id DESC").fetchone()[0], "REFUND")
        cashier = app.test_client()
        cashier.post("/merchant/login", data={"csrf_token": self.csrf(cashier, "/merchant/verify"),
                                              "merchant_id": merchant_id, "pin": app.config["CASHIER_PIN"]})
        page = cashier.get(f"/merchant/verify?code={order['voucher_code']}").get_data(as_text=True)
        self.assertIn("Refunded. Do not hand over the gift.", page)
        self.assertIn("Refunded to the sender", self.client.get(f"/voucher/{order['voucher_code']}").get_data(as_text=True))
        # a second refund is refused
        self.assertIn("Only paid gifts", self.refund(order["reference"]).get_data(as_text=True))

    def test_collected_gift_cannot_be_refunded(self):
        order, _ = self.buy()
        db = self.db()
        db.execute("UPDATE vouchers SET status='USED'")
        db.commit()
        self.assertIn("already collected", self.refund(order["reference"]).get_data(as_text=True))
        self.assertEqual(self.db().execute("SELECT status FROM orders").fetchone()[0], "PAID")

    def test_pesapal_refund_is_requested_with_confirmation_code(self):
        order, _ = self.buy()
        db = self.db()
        db.execute("UPDATE orders SET provider='pesapal', confirmation_code='MP123'")
        db.commit()
        fake = FakePesapal()
        m.pesapal_client = lambda: fake
        body = self.refund(order["reference"]).get_data(as_text=True)
        self.assertIn("Approve it in the Pesapal dashboard", body)
        self.assertEqual(fake.calls[0]["confirmation_code"], "MP123")
        self.assertEqual(fake.calls[0]["amount"], order["amount"])

    def test_pesapal_rejection_keeps_the_gift_active(self):
        order, _ = self.buy()
        db = self.db()
        db.execute("UPDATE orders SET provider='pesapal', confirmation_code='MP123'")
        db.commit()
        m.pesapal_client = lambda: FakePesapal(fail=True)
        self.assertIn("still active", self.refund(order["reference"]).get_data(as_text=True))
        self.assertEqual(self.db().execute("SELECT status FROM vouchers").fetchone()[0], "ACTIVE")
        self.assertEqual(self.db().execute("SELECT status FROM orders").fetchone()[0], "PAID")


class MigrationTests(unittest.TestCase):
    def test_old_database_gains_refunded_status_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            init_db(path)
            seed_db(path)
            db = connect(path)
            # put the old CHECK back to simulate a database from before this change
            for table, new, old in (("vouchers", "'ACTIVE', 'USED', 'REFUNDED'", "'ACTIVE', 'USED'"),
                                    ("orders", "'PENDING', 'PAID', 'FAILED', 'REFUNDED'", "'PENDING', 'PAID', 'FAILED'")):
                m._widen_check(db, table, new, old)
            pid, mid = db.execute("SELECT id, merchant_id FROM products LIMIT 1").fetchone()
            db.execute("INSERT INTO vouchers (voucher_code, merchant_id, product_id, sender_name, recipient_phone, "
                       "monetary_value) VALUES ('GIFT-AAAA-BBBB', ?, ?, 'A', '+256770123456', 1000)", (mid, pid))
            db.commit()
            db.close()
            init_db(path)
            db = connect(path)
            db.execute("UPDATE vouchers SET status='REFUNDED'")
            db.commit()
            self.assertEqual(db.execute("SELECT COUNT(*) FROM vouchers").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
