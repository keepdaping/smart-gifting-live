"""Pesapal API 3.0 client (MTN Mobile Money, Airtel Money and cards in Uganda).

How a payment works
-------------------
1. ``submit_order`` tells Pesapal what is being paid for. Pesapal returns an
   ``order_tracking_id`` and a ``redirect_url``.
2. The sender is sent to ``redirect_url``. On Pesapal's page they pick MTN,
   Airtel or card and approve the prompt on their phone.
3. Pesapal calls our IPN URL and sends the sender back to our callback URL.
   Neither of those calls proves payment by itself, so we always ask
   ``transaction_status`` and only trust its answer.

Only the standard library is used, so no extra package is needed.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

TOKEN_REUSE_SECONDS = 240

BASE_URLS = {
    "sandbox": "https://cybqa.pesapal.com/pesapalv3",
    "live": "https://pay.pesapal.com/v3",
}

# status_code values returned by GetTransactionStatus
INVALID, COMPLETED, FAILED, REVERSED = 0, 1, 2, 3


class PesapalError(RuntimeError):
    """Pesapal could not be reached or rejected the request."""


@dataclass
class Order:
    tracking_id: str
    redirect_url: str


@dataclass
class Status:
    code: int                  # one of INVALID, COMPLETED, FAILED, REVERSED (-1 if unknown)
    description: str           # e.g. "Completed", "Failed"
    amount: float
    currency: str
    method: str                # e.g. "MTN UG", "Airtel UG", "Visa"
    merchant_reference: str
    confirmation_code: str

    @property
    def completed(self) -> bool:
        return self.code == COMPLETED

    @property
    def final_failure(self) -> bool:
        # INVALID is also what Pesapal reports while the sender has not paid yet,
        # so it is not treated as final; only FAILED and REVERSED are.
        return self.code in (FAILED, REVERSED)


def _urllib_transport(method, url, headers, body, timeout):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise PesapalError(f"Could not reach Pesapal: {exc}") from exc


class PesapalClient:
    def __init__(self, consumer_key, consumer_secret, environment="sandbox",
                 transport=None, timeout=20):
        if environment not in BASE_URLS:
            raise ValueError("environment must be 'sandbox' or 'live'")
        if not consumer_key or not consumer_secret:
            raise ValueError("Pesapal consumer key and secret are required")
        self.key = consumer_key
        self.secret = consumer_secret
        self.environment = environment
        self.base = BASE_URLS[environment]
        self.transport = transport or _urllib_transport
        self.timeout = timeout
        self._token = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    # -- low level -----------------------------------------------------------
    def _call(self, method, path, body=None, auth=True):
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self.token()}"
        status, raw = self.transport(method, self.base + path, headers, body, self.timeout)
        try:
            data = json.loads(raw or b"{}")
        except ValueError as exc:
            raise PesapalError(f"Pesapal returned an unreadable reply (HTTP {status})") from exc
        error = data.get("error") if isinstance(data, dict) else None
        if status >= 400 or (error and (error.get("message") or error.get("code"))):
            message = (error or {}).get("message") or data.get("message") or f"HTTP {status}"
            raise PesapalError(f"Pesapal error: {message}")
        return data

    def token(self):
        """Bearer token, reused until shortly before it expires (Pesapal gives ~5 minutes)."""
        with self._lock:
            if self._token and time.time() < self._token_expires:
                return self._token
            data = self._call("POST", "/api/Auth/RequestToken",
                              {"consumer_key": self.key, "consumer_secret": self.secret}, auth=False)
            token = data.get("token")
            if not token:
                raise PesapalError("Pesapal did not return a token. Check the consumer key and secret.")
            # Tokens last about 5 minutes; refresh after 4 to stay safely inside that.
            self._token, self._token_expires = token, time.time() + TOKEN_REUSE_SECONDS
            return token

    # -- API -----------------------------------------------------------------
    def register_ipn(self, url, notification_type="GET"):
        """Register our IPN URL once; returns the ipn_id used in every order."""
        data = self._call("POST", "/api/URLSetup/RegisterIPN",
                          {"url": url, "ipn_notification_type": notification_type})
        ipn_id = data.get("ipn_id")
        if not ipn_id:
            raise PesapalError("Pesapal did not return an IPN id.")
        return ipn_id

    def submit_order(self, *, reference, amount, description, callback_url, ipn_id,
                     phone=None, email=None, first_name="", last_name="", currency="UGX"):
        billing = {"country_code": "UG", "first_name": first_name[:50], "last_name": last_name[:50]}
        if phone:
            billing["phone_number"] = phone
        if email:
            billing["email_address"] = email
        data = self._call("POST", "/api/Transactions/SubmitOrderRequest", {
            "id": reference,
            "currency": currency,
            "amount": amount,
            "description": description[:100],
            "callback_url": callback_url,
            "notification_id": ipn_id,
            "billing_address": billing,
        })
        tracking_id, redirect_url = data.get("order_tracking_id"), data.get("redirect_url")
        if not tracking_id or not redirect_url:
            raise PesapalError("Pesapal did not return a payment page.")
        return Order(tracking_id=tracking_id, redirect_url=redirect_url)

    def transaction_status(self, tracking_id):
        query = urllib.parse.urlencode({"orderTrackingId": tracking_id})
        data = self._call("GET", f"/api/Transactions/GetTransactionStatus?{query}")
        try:
            code = int(data.get("status_code"))
        except (TypeError, ValueError):
            code = -1
        return Status(
            code=code,
            description=str(data.get("payment_status_description") or ""),
            amount=float(data.get("amount") or 0),
            currency=str(data.get("currency") or ""),
            method=str(data.get("payment_method") or ""),
            merchant_reference=str(data.get("merchant_reference") or ""),
            confirmation_code=str(data.get("confirmation_code") or ""),
        )

    def refund(self, *, confirmation_code, amount, username, remarks):
        """Ask Pesapal to refund a COMPLETED payment. Mobile Money can only be refunded in full,
        and only one refund request is allowed per payment. Pesapal still needs the merchant
        to approve it in the dashboard before the money moves."""
        data = self._call("POST", "/api/Transactions/RefundRequest", {
            "confirmation_code": confirmation_code,
            "amount": str(amount),
            "username": username[:50],
            "remarks": remarks[:200],
        })
        code = str(data.get("status") or data.get("error") or "")
        if code and code != "200":
            raise PesapalError(f"Pesapal refused the refund: {data.get('message') or code}")
        return str(data.get("message") or "Refund request received")
