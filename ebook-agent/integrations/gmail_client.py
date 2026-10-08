"""Gmail sending. Each message is created as a draft and sent from that draft, using the gmail.compose scope.

The access-link email on purchase is sent this way, so every buyer link has a Gmail draft and a sent copy.
"""

from __future__ import annotations

import base64
import logging
from email.message import EmailMessage

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from integrations.google_auth import GoogleAuthError, get_credentials

logger = logging.getLogger(__name__)


class GmailError(RuntimeError):
    """A Gmail call failed, or Gmail is not authorized."""


def _service():
    try:
        creds = get_credentials()
    except GoogleAuthError as exc:
        raise GmailError(str(exc)) from exc
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_raw_message(*, to: str, subject: str, text: str, headers: dict[str, str] | None = None) -> str:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(text)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def create_draft_and_send(*, to: str, subject: str, text: str, headers: dict[str, str] | None = None) -> str:
    """Create a Gmail draft, then send that draft. Returns the id of the sent message."""
    raw = build_raw_message(to=to, subject=subject, text=text, headers=headers)
    service = _service()
    try:
        draft = service.users().drafts().create(userId="me", body={"message": {"raw": raw}}).execute()
        sent = service.users().drafts().send(userId="me", body={"id": draft["id"]}).execute()
    except HttpError as exc:
        raise GmailError(f"Gmail API error {exc.resp.status}: {exc.reason}") from exc
    return str(sent.get("id", ""))
