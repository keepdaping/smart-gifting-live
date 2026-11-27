"""Admin panel: real partner stores, their menus and photos, PINs, refunds and payouts.

Registered by app.py with ``admin_panel.register(app_module)``, so it reuses the app's
database helpers without a circular import.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

from flask import abort, flash, redirect, render_template, request, session, url_for
from werkzeug.security import generate_password_hash

ADMIN_MAX_FAILURES = 5
ADMIN_WINDOW_MINUTES = 15
CATEGORY_ICONS = {
    "Food": "bi-egg-fried", "Fast Food": "bi-egg-fried", "Coffee": "bi-cup-hot", "Bakery": "bi-cake2",
    "Desserts": "bi-cake", "Pizza": "bi-circle-half", "Fashion": "bi-bag", "Supermarket": "bi-basket",
    "Groceries": "bi-basket", "Electronics": "bi-phone", "Pharmacy": "bi-capsule", "Books": "bi-book",
    "Flowers": "bi-flower1", "Beauty": "bi-stars",
}


def register(m):
    app = m.app

    # -- helpers ---------------------------------------------------------------
    def enabled():
        return bool(app.config.get("ADMIN_PASSWORD"))

    def is_admin():
        return enabled() and session.get("admin") is True

    def guard():
        """None when the admin may continue, otherwise the response to return."""
        if not enabled():
            abort(404)
        if not is_admin():
            return redirect(url_for("admin_home"))
        return None

    def blocked(db, key):
        since = (datetime.now(timezone.utc) - timedelta(minutes=ADMIN_WINDOW_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
        return db.execute("SELECT COUNT(*) FROM login_failures WHERE merchant_id=0 AND client_key=? "
                          "AND created_at>=?", (key, since)).fetchone()[0] >= ADMIN_MAX_FAILURES

    def text(name, limit=200):
        return (request.form.get(name) or "").strip()[:limit]

    def parse_price(raw):
        digits = re.sub(r"[^\d]", "", raw or "")
        return int(digits) if digits else 0

    # -- sign in ---------------------------------------------------------------
    @app.route("/admin", methods=["GET"])
    def admin_home():
        if not enabled():
            abort(404)
        if not is_admin():
            return render_template("admin/login.html")
        db = m.get_db()
        stores = db.execute("""
            SELECT m.*, (SELECT COUNT(*) FROM menu_items mi WHERE mi.merchant_id=m.id AND mi.is_active=1) AS items
            FROM merchants m ORDER BY m.is_active DESC, m.name""").fetchall()
        balances = {s["id"]: m.store_sales(db, s["id"]) for s in stores}
        totals = db.execute("""
            SELECT
              (SELECT COUNT(*) FROM orders WHERE status IN ('PAID','REFUNDED')) AS orders,
              (SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='PAID') AS takings,
              (SELECT COUNT(*) FROM vouchers WHERE status='ACTIVE') AS waiting,
              (SELECT COUNT(*) FROM vouchers WHERE status='USED') AS collected,
              (SELECT COUNT(*) FROM orders WHERE status='REFUNDED') AS refunded""").fetchone()
        return render_template("admin/home.html", stores=stores, balances=balances, totals=totals,
                               payment_mode=m.payment_provider(), sms_mode=app.config["SMS_PROVIDER"],
                               demo_seeded=app.config["SEED_DEMO_STORES"])

    @app.route("/admin/login", methods=["POST"])
    def admin_login():
        if not enabled():
            abort(404)
        db = m.get_db()
        key = m.client_rate_limit_key()
        if blocked(db, key):
            flash(f"Too many wrong passwords. Wait {ADMIN_WINDOW_MINUTES} minutes.", "danger")
            return redirect(url_for("admin_home"))
        password = request.form.get("password", "")
        if secrets.compare_digest(password.encode(), app.config["ADMIN_PASSWORD"].encode()):
            db.execute("DELETE FROM login_failures WHERE merchant_id=0 AND client_key=?", (key,))
            db.commit()
            session.clear()
            session["admin"] = True
            return redirect(url_for("admin_home"))
        db.execute("INSERT INTO login_failures (merchant_id, client_key, created_at) VALUES (0, ?, ?)",
                   (key, m.utc_now()))
        db.commit()
        flash("Wrong password.", "danger")
        return redirect(url_for("admin_home"))

    @app.route("/admin/logout", methods=["POST"])
    def admin_logout():
        session.pop("admin", None)
        return redirect(url_for("index"))

    # -- stores ----------------------------------------------------------------
    def store_form_errors(db, store_id=None):
        errors = {}
        name, category = text("name", 80), text("category", 40)
        if len(name) < 2:
            errors["name"] = "Enter the store's name."
        elif db.execute("SELECT 1 FROM merchants WHERE lower(name)=lower(?) AND id IS NOT ?",
                        (name, store_id)).fetchone():
            errors["name"] = "Another store already has this name."
        if len(category) < 2:
            errors["category"] = "Enter a category, e.g. Coffee or Bakery."
        phone = text("payout_phone", 20)
        if phone and not m.normalise_phone(phone):
            errors["payout_phone"] = "Enter a Ugandan mobile number, e.g. 0770 123 456."
        pin = text("pin", 12)
        if pin and not re.fullmatch(r"\d{6,12}", pin):
            errors["pin"] = "The PIN must be 6 to 12 digits."
        return errors

    def save_store(db, store_id=None):
        name, category = text("name", 80), text("category", 40)
        values = {
            "name": name, "category": category, "description": text("description", 300),
            "location": text("location", 120),
            "payout_phone": m.normalise_phone(text("payout_phone", 20)) or None,
            "is_active": 1 if request.form.get("is_active") else 0,
            "icon": CATEGORY_ICONS.get(category, "bi-shop"),
        }
        if store_id is None:
            cur = db.execute(
                "INSERT INTO merchants (name, category, description, location, payout_phone, is_active, icon, accent) "
                "VALUES (:name, :category, :description, :location, :payout_phone, :is_active, :icon, '#16845b')",
                values)
            store_id = cur.lastrowid
        else:
            db.execute("UPDATE merchants SET name=:name, category=:category, description=:description, "
                       "location=:location, payout_phone=:payout_phone, is_active=:is_active WHERE id=:id",
                       dict(values, id=store_id))
        photo = m.save_uploaded_image(db, request.files.get("photo"))
        if photo:
            db.execute("UPDATE merchants SET image_url=? WHERE id=?",
                       (url_for("stored_image", image_id=int(photo[3:])), store_id))
        pin = text("pin", 12)
        if pin:
            db.execute("UPDATE merchants SET cashier_pin_hash=? WHERE id=?", (generate_password_hash(pin), store_id))
        db.commit()
        return store_id

    @app.route("/admin/store/new", methods=["GET", "POST"])
    def admin_store_new():
        if (r := guard()):
            return r
        db = m.get_db()
        errors = {}
        if request.method == "POST":
            errors = store_form_errors(db)
            if not errors:
                try:
                    store_id = save_store(db)
                except m.ImageError as exc:
                    flash(str(exc), "danger")
                else:
                    flash("Store added. Now add the gifts it sells.", "success")
                    return redirect(url_for("admin_store", store_id=store_id))
        return render_template("admin/store.html", store=None, items=[], errors=errors, form=request.form,
                               categories=_categories(db), sales=None)

    @app.route("/admin/store/<int:store_id>", methods=["GET", "POST"])
    def admin_store(store_id):
        if (r := guard()):
            return r
        db = m.get_db()
        store = db.execute("SELECT * FROM merchants WHERE id=?", (store_id,)).fetchone()
        if store is None:
            abort(404)
        errors = {}
        if request.method == "POST":
            errors = store_form_errors(db, store_id)
            if not errors:
                try:
                    save_store(db, store_id)
                except m.ImageError as exc:
                    flash(str(exc), "danger")
                else:
                    flash("Store saved.", "success")
                    return redirect(url_for("admin_store", store_id=store_id))
        items = db.execute("SELECT * FROM menu_items WHERE merchant_id=? ORDER BY is_active DESC, category, name",
                           (store_id,)).fetchall()
        return render_template("admin/store.html", store=store, items=items, errors=errors,
                               form=request.form if request.method == "POST" else store,
                               categories=_categories(db), sales=m.store_sales(db, store_id),
                               item_image=m.menu_item_image)

    def _categories(db):
        return [r[0] for r in db.execute("SELECT DISTINCT category FROM merchants ORDER BY category")]

    # -- gifts on sale -----------------------------------------------------------
    def item_values():
        errors, price = {}, parse_price(request.form.get("price_ugx"))
        name, section = text("name", 80), text("category", 40)
        if len(name) < 2:
            errors["name"] = "Enter the gift's name."
        if not section:
            errors["category"] = "Enter a menu section, e.g. Cakes."
        if not 500 <= price <= 5_000_000:
            errors["price_ugx"] = "Enter a price between UGX 500 and UGX 5,000,000."
        values = {"name": name, "category": section or "Gifts", "variant": text("variant", 60) or None,
                  "description": text("description", 300), "price_ugx": price,
                  "tags": text("tags", 200), "is_active": 1 if request.form.get("is_active", "1") else 0}
        return values, errors

    @app.route("/admin/store/<int:store_id>/items", methods=["POST"])
    def admin_item_add(store_id):
        if (r := guard()):
            return r
        db = m.get_db()
        if not db.execute("SELECT 1 FROM merchants WHERE id=?", (store_id,)).fetchone():
            abort(404)
        values, errors = item_values()
        if errors:
            for message in errors.values():
                flash(message, "danger")
            return redirect(url_for("admin_store", store_id=store_id) + "#addItem")
        try:
            photo = m.save_uploaded_image(db, request.files.get("photo"))
        except m.ImageError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("admin_store", store_id=store_id) + "#addItem")
        try:
            db.execute("INSERT INTO menu_items (merchant_id, category, name, variant, description, price_ugx, tags, "
                       "image_filename, is_active, price_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_DATE)",
                       (store_id, values["category"], values["name"], values["variant"], values["description"],
                        values["price_ugx"], values["tags"], photo))
            db.commit()
        except m.sqlite3.IntegrityError:
            flash("This store already sells a gift with that name and option.", "danger")
            return redirect(url_for("admin_store", store_id=store_id) + "#addItem")
        flash(f"Added {values['name']}.", "success")
        return redirect(url_for("admin_store", store_id=store_id) + "#items")

    @app.route("/admin/item/<int:item_id>", methods=["GET", "POST"])
    def admin_item(item_id):
        if (r := guard()):
            return r
        db = m.get_db()
        item = db.execute("SELECT mi.*, m.name AS store_name FROM menu_items mi JOIN merchants m ON m.id=mi.merchant_id "
                          "WHERE mi.id=?", (item_id,)).fetchone()
        if item is None:
            abort(404)
        errors = {}
        if request.method == "POST":
            values, errors = item_values()
            values["is_active"] = 1 if request.form.get("is_active") else 0
            if not errors:
                try:
                    photo = m.save_uploaded_image(db, request.files.get("photo"))
                    price_changed = values["price_ugx"] != item["price_ugx"]
                    db.execute("UPDATE menu_items SET category=?, name=?, variant=?, description=?, price_ugx=?, "
                               "tags=?, is_active=?, image_filename=COALESCE(?, image_filename)"
                               + (", price_updated_at=CURRENT_DATE" if price_changed else "") + " WHERE id=?",
                               (values["category"], values["name"], values["variant"], values["description"],
                                values["price_ugx"], values["tags"], values["is_active"], photo, item_id))
                    db.commit()
                except m.ImageError as exc:
                    flash(str(exc), "danger")
                except m.sqlite3.IntegrityError:
                    flash("This store already sells a gift with that name and option.", "danger")
                else:
                    flash("Gift saved. Gifts already bought keep the price that was paid.", "success")
                    return redirect(url_for("admin_store", store_id=item["merchant_id"]) + "#items")
        return render_template("admin/item.html", item=item, errors=errors,
                               form=request.form if request.method == "POST" else item,
                               image=m.menu_item_image(item["image_filename"]))

    # -- payouts ---------------------------------------------------------------
    @app.route("/admin/store/<int:store_id>/payout", methods=["POST"])
    def admin_payout(store_id):
        if (r := guard()):
            return r
        db = m.get_db()
        if not db.execute("SELECT 1 FROM merchants WHERE id=?", (store_id,)).fetchone():
            abort(404)
        amount = parse_price(request.form.get("amount"))
        if amount <= 0:
            flash("Enter the amount you paid the store.", "danger")
        else:
            db.execute("INSERT INTO payouts (merchant_id, amount, reference, note, paid_at) VALUES (?, ?, ?, ?, ?)",
                       (store_id, amount, text("reference", 60), text("note", 200), m.utc_now()))
            db.commit()
            flash(f"Recorded a payout of {m.ugx_filter(amount)}.", "success")
        return redirect(url_for("admin_store", store_id=store_id) + "#payouts")

    # -- gifts and refunds -----------------------------------------------------
    @app.route("/admin/gifts")
    def admin_gifts():
        if (r := guard()):
            return r
        db = m.get_db()
        query = (request.args.get("q") or "").strip()
        sql = """SELECT o.*, m.name AS store_name, v.status AS voucher_status, v.redeemed_at
                 FROM orders o JOIN merchants m ON m.id=o.merchant_id
                 LEFT JOIN vouchers v ON v.voucher_code=o.voucher_code
                 WHERE o.status IN ('PAID','REFUNDED')"""
        args = []
        if query:
            digits = re.sub(r"\D", "", query)[-9:]
            sql += " AND (o.reference LIKE ? OR o.voucher_code LIKE ? OR o.sender_name LIKE ?"
            args += [f"%{query}%", f"%{query.upper()}%", f"%{query}%"]
            if digits:
                sql += " OR o.sender_phone LIKE ? OR o.recipient_phone LIKE ?"
                args += [f"%{digits}%", f"%{digits}%"]
            sql += ")"
        orders = db.execute(sql + " ORDER BY o.id DESC LIMIT 100", args).fetchall()
        return render_template("admin/gifts.html", orders=orders, q=query)

    @app.route("/admin/gifts/<reference>/refund", methods=["POST"])
    def admin_refund(reference):
        if (r := guard()):
            return r
        db = m.get_db()
        try:
            note = m.refund_order(db, reference, by="Smart Gifting admin",
                                  remarks=text("reason", 150) or "Gift not collected")
        except m.RefundError as exc:
            flash(str(exc), "danger")
        else:
            flash(f"Refund started. {note}", "success")
        return redirect(url_for("admin_gifts", q=reference))

    @app.route("/admin/sms")
    def admin_sms():
        if (r := guard()):
            return r
        rows = m.get_db().execute("SELECT * FROM sms_outbox ORDER BY id DESC LIMIT 100").fetchall()
        return render_template("sms_outbox.html", rows=rows, provider=app.config["SMS_PROVIDER"], admin=True)
