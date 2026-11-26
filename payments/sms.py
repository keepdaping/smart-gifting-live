"""Text messages through Africa's Talking, with a simulated fallback.

Modes
-----
* ``africastalking``: real SMS. Use username ``sandbox`` with a sandbox API key
  to test (messages appear in the Africa's Talking simulator, not on phones),
  or your live username and key to reach real phones.
* ``simulated``: nothing is sent. Messages are only recorded in the outbox
  table so you can read them while testing.

Only the standard library is used.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

HOSTS = {
    "sandbox": "https://api.sandbox.africastalking.com/version1/messaging",
    "live": "https://api.africastalking.com/version1/messaging",
}
SUCCESS_CODES = {100, 101, 102}  # Processed, Sent, Queued


class SmsError(RuntimeError):
    """The SMS provider could not be reached or refused the message."""


@dataclass
class SmsResult:
    status: str          # "SENT", "SIMULATED" or "FAILED"
    provider_id: str = ""
    cost: str = ""
    error: str = ""


class AfricasTalking:
    def __init__(self, username: str, api_key: str, sender_id: str = "", timeout: float = 10.0):
        if not username or not api_key:
            raise ValueError("Africa's Talking needs a username and an API key")
        self.username = username
        self.api_key = api_key
        self.sender_id = sender_id
        self.timeout = timeout
        self.url = HOSTS["sandbox" if username == "sandbox" else "live"]

    def send(self, to: str, message: str) -> SmsResult:
        form = {"username": self.username, "to": to, "message": message}
        if self.sender_id:
            form["from"] = self.sender_id
        request = urllib.request.Request(
            self.url,
            data=urllib.parse.urlencode(form).encode("utf-8"),
            headers={"apiKey": self.api_key, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise SmsError(f"Africa's Talking returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise SmsError(f"Africa's Talking could not be reached: {exc}") from exc
        return parse_response(body)


def parse_response(body: dict) -> SmsResult:
    data = (body or {}).get("SMSMessageData") or {}
    recipients = data.get("Recipients") or []
    if not recipients:
        return SmsResult("FAILED", error=str(data.get("Message") or "No recipient accepted"))
    first = recipients[0]
    try:
        code = int(first.get("statusCode", 0))
    except (TypeError, ValueError):
        code = 0
    if code in SUCCESS_CODES or str(first.get("status", "")).lower() == "success":
        return SmsResult("SENT", provider_id=str(first.get("messageId", "")), cost=str(first.get("cost", "")))
    return SmsResult("FAILED", error=str(first.get("status") or f"status code {code}"))
