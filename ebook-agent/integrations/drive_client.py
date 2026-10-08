"""Google Drive upload for product files. Uses drive.file, so the app can only see files it created."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from config import settings
from integrations.google_auth import GoogleAuthError, get_credentials


class DriveError(RuntimeError):
    """A Drive call failed, or Drive is not authorized."""


def upload_file(path: Path, *, name: str | None = None) -> dict[str, Any]:
    if not path.is_file():
        raise DriveError(f"File not found: {path}")
    try:
        creds = get_credentials()
    except GoogleAuthError as exc:
        raise DriveError(str(exc)) from exc
    service = build("drive", "v3", credentials=creds, cache_discovery=False)
    body: dict[str, Any] = {"name": name or path.name}
    if settings.google_drive_folder_id:
        body["parents"] = [settings.google_drive_folder_id]
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
    try:
        created = service.files().create(body=body, media_body=media, fields="id,name,webViewLink").execute()
    except HttpError as exc:
        raise DriveError(f"Drive API error {exc.resp.status}: {exc.reason}") from exc
    return {"id": created["id"], "name": created.get("name"), "web_view_link": created.get("webViewLink")}
