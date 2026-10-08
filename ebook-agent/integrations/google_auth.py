"""Google OAuth for Gmail and Drive. One token file serves both clients.

One-time setup on a machine with a browser (run it on the host, not inside Docker):

    python -m integrations.google_auth

It reads secrets/google_client_secret.json and writes secrets/google_token.json.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config import settings

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.compose",  # create drafts and send them (drafts.send needs this scope)
    "https://www.googleapis.com/auth/drive.file",  # upload files this app creates
]


class GoogleAuthError(RuntimeError):
    """Google credentials are missing, invalid, or cannot be refreshed."""


def get_credentials() -> Credentials:
    token_path = settings.google_token_file
    if not token_path.exists():
        raise GoogleAuthError(f"No Google token at {token_path}. Run: python -m integrations.google_auth")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as exc:  # revoked or expired refresh token
            raise GoogleAuthError(f"Could not refresh the Google token ({exc}). Re-run: python -m integrations.google_auth") from exc
        token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds
    raise GoogleAuthError("The Google token is invalid and has no refresh token. Re-run: python -m integrations.google_auth")


def authorize(port: int = 0) -> Path:
    """Interactive consent flow. Opens a browser on the machine that runs it."""
    secrets_path = settings.google_client_secrets_file
    if not secrets_path.exists():
        raise GoogleAuthError(f"Missing OAuth client file: {secrets_path}. Download it from Google Cloud Console.")
    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), SCOPES)
    creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")
    token_path = settings.google_token_file
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token_path, 0o600)
    except OSError:
        pass
    return token_path


def main() -> None:
    logging.basicConfig(level="INFO", format="%(levelname)s %(message)s")
    path = authorize()
    print(f"Google token saved to {path}")


if __name__ == "__main__":
    main()
