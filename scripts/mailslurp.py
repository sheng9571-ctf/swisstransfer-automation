import re
import time
from datetime import datetime, timezone
from email.utils import parseaddr
from urllib.parse import quote

import requests

from scripts.common import AutomationError

SUBJECT = re.compile(r"^Swisstransfer confirmation code:\s*([A-Z0-9]{6})\s*$", re.I)


def extract_code(email, address, since):
    sender = parseaddr(email.get("from") or "")[1].lower()
    recipients = email.get("to") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    if sender != "noreply@swisstransfer.com" or address.lower() not in [
            parseaddr(recipient)[1].lower() for recipient in recipients]:
        return None
    try:
        created = datetime.fromisoformat(email["createdAt"].replace("Z", "+00:00"))
        if created.tzinfo is None or created < since:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    match = SUBJECT.fullmatch(email.get("subject") or "")
    return match.group(1).upper() if match else None


class MailSlurp:
    def __init__(self, key, inbox_id, address):
        self.client = requests.Session()
        self.client.headers["x-api-key"] = key
        self.inbox_id, self.address = inbox_id, address
        self.path = "/inboxes/" + quote(inbox_id, safe="")

    def close(self):
        self.client.close()

    def get(self, path, params=None):
        try:
            response = self.client.get("https://api.mailslurp.com" + path,
                                       params=params, timeout=(10, 30),
                                       allow_redirects=False)
        except requests.RequestException:
            raise AutomationError("MailSlurp network request failed") from None
        with response:
            if response.status_code == 429:
                raise AutomationError("MailSlurp API rate limit reached")
            if response.status_code != 200:
                raise AutomationError(f"MailSlurp API failed (HTTP {response.status_code})")
            try:
                return response.json()
            except ValueError:
                raise AutomationError("MailSlurp returned invalid JSON") from None

    def prepare(self):
        inbox = self.get(self.path)
        if (inbox.get("emailAddress") or "").lower() != self.address.lower():
            raise AutomationError("SWISS_EMAIL does not match MAILSLURP_INBOX_ID")
        # A narrow time window plus old message IDs excludes pre-existing OTPs.
        since = datetime.now(timezone.utc)
        existing = self.get(self.path + "/emails", {
            "since": since.isoformat(), "sort": "DESC", "size": 100,
        })
        if not isinstance(existing, list) or len(existing) >= 100:
            raise AutomationError("MailSlurp email window is too busy to select an OTP reliably")
        return since, {email["id"] for email in existing}

    def wait_code(self, since, existing, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            emails = self.get(self.path + "/emails", {
                "since": since.isoformat(), "sort": "ASC", "size": 100,
            })
            if not isinstance(emails, list):
                raise AutomationError("MailSlurp returned an invalid email list")
            if len(emails) >= 100:
                raise AutomationError("MailSlurp email window is too busy to select an OTP reliably")
            for preview in emails:
                message_id = preview.get("id")
                if not message_id or message_id in existing:
                    continue
                existing.add(message_id)
                email = self.get("/emails/" + quote(message_id, safe=""))
                code = extract_code(email, self.address, since)
                if code:
                    return code
            time.sleep(min(3, max(0, deadline - time.monotonic())))
        raise AutomationError("Timed out waiting for this run's SwissTransfer verification email")
