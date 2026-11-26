"""Open-a-gift, legal pages, WhatsApp help and the recipient's unwrap screen."""
import os
import tempfile
import unittest


class StorefrontV2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        import app as appmod
        self.appmod = appmod
        self.old = {k: appmod.app.config.get(k) for k in ("DATABASE", "SUPPORT_WHATSAPP")}
        appmod.app.config.update(TESTING=True, DATABASE=self.db_path, SUPPORT_WHATSAPP="")
        appmod.init_db(self.db_path)
        appmod.seed_db(self.db_path)
        self.client = appmod.app.test_client()

    def tearDown(self):
        self.appmod.app.config.update(self.old)
        self.tmp.cleanup()

    def _voucher_code(self):
        db = self.appmod.connect(self.db_path)
        row = db.execute("SELECT id, merchant_id FROM products LIMIT 1").fetchone()
        code = self.appmod.generate_voucher_code()
        cols = [r[1] for r in db.execute("PRAGMA table_info(vouchers)")]
        values = {"voucher_code": code, "product_id": row[0], "sender_name": "Amina",
                  "recipient_phone": "+256770123456", "status": "ACTIVE"}
        for optional, value in (("merchant_id", row[1]), ("monetary_value", 10000),
                                ("price_at_purchase", 10000), ("created_at", "2026-11-26T00:00:00")):
            if optional in cols:
                values[optional] = value
        db.execute(f"INSERT INTO vouchers ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
                   list(values.values()))
        db.commit()
        return code

    def test_home_has_occasions_claim_and_faq(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('class="occasions"', html)
        self.assertIn('id="claim"', html)
        self.assertIn('id="help"', html)
        self.assertNotIn("wa-help", html)

    def test_whatsapp_button_only_when_configured(self):
        self.appmod.app.config["SUPPORT_WHATSAPP"] = "256770123456"
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn("https://wa.me/256770123456", html)

    def test_legal_pages(self):
        self.assertIn("Data Protection", self.client.get("/privacy").get_data(as_text=True))
        self.assertIn("Refunds", self.client.get("/terms").get_data(as_text=True))

    def test_claim_redirects_to_gift_and_forgives_formatting(self):
        code = self._voucher_code()
        typed = code.replace("-", " ").lower()
        resp = self.client.get("/claim", query_string={"code": typed})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp.headers["Location"].endswith(f"/voucher/{code}"))

    def test_unknown_code_goes_back_to_form(self):
        resp = self.client.get("/claim", query_string={"code": "GIFT-ZZZZ-ZZZZ"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("#claim", resp.headers["Location"])

    def test_recipient_sees_unwrap_sender_does_not(self):
        code = self._voucher_code()
        self.assertIn('id="unwrap"', self.client.get(f"/voucher/{code}").get_data(as_text=True))
        with self.client.session_transaction() as s:
            s["sent_codes"] = [code]
        self.assertNotIn('id="unwrap"', self.client.get(f"/voucher/{code}").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
