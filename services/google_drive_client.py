"""Google Drive client for uploading transcript and summary text files"""
import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)


class GoogleDriveUploadError(Exception):
    pass


class GoogleDriveClient:
    """Uploads text files to Google Drive using a service account."""

    def __init__(self):
        self._service = None

    def _build_service(self):
        try:
            from google.oauth2.service_account import Credentials
            from googleapiclient.discovery import build
        except ImportError:
            raise RuntimeError(
                "Google Drive dependencies not installed. "
                "Run: pip install google-api-python-client google-auth"
            )

        sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        sa_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")

        if sa_json:
            info = json.loads(sa_json)
            creds = Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/drive"]
            )
        elif sa_file:
            creds = Credentials.from_service_account_file(
                sa_file, scopes=["https://www.googleapis.com/auth/drive"]
            )
        else:
            raise RuntimeError(
                "No Google credentials configured. "
                "Set GOOGLE_SERVICE_ACCOUNT_JSON (JSON string) or GOOGLE_SERVICE_ACCOUNT_FILE (path)."
            )

        return build("drive", "v3", credentials=creds, cache_discovery=False)

    def _get_service(self):
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _upload_sync(self, folder_id: str, filename: str, content: str) -> dict:
        from googleapiclient.http import MediaInMemoryUpload

        service = self._get_service()
        metadata = {
            "name": filename,
            "parents": [folder_id],
            "mimeType": "text/plain",
        }
        media = MediaInMemoryUpload(
            content.encode("utf-8"),
            mimetype="text/plain; charset=utf-8",
            resumable=False,
        )
        file = (
            service.files()
            .create(body=metadata, media_body=media, fields="id,name,webViewLink")
            .execute()
        )
        return file

    async def upload_text(self, folder_id: str, filename: str, content: str) -> dict:
        """Upload a text string as a file to Google Drive.

        Returns a dict with keys: id, name, webViewLink.
        Runs the synchronous Google client in a thread executor so it doesn't
        block the async event loop.
        """
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(
                None, self._upload_sync, folder_id, filename, content
            )
            return result
        except Exception as e:
            raise GoogleDriveUploadError(f"drive_upload_failed: {e}") from e
