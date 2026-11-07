"""Private artifacts and conditional job manifests in the existing container."""

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas


def conversation_scope(conversation: str) -> str:
    if not isinstance(conversation, str) or not conversation.strip():
        raise ValueError("A conversation is required.")
    return hashlib.sha256(conversation.encode()).hexdigest()[:32]


def validate_blob_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_./-]{1,512}", name) or any(
        part in ("", ".", "..") for part in name.split("/")
    ):
        raise ValueError("Invalid artifact name.")
    return name


class WorkspaceDeletedError(RuntimeError):
    pass


def workspace_artifact_name(name: str, scope: str | None) -> str:
    validate_blob_name(name)
    if scope is None:
        return name
    if not re.fullmatch(r"[a-f0-9]{32}", scope):
        raise ValueError("Invalid workspace artifact scope.")
    basename = Path(name).name
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.(png|jpg|webp|blend|glb)", basename):
        raise ValueError("Invalid workspace artifact filename.")
    return f"workspace-artifacts/{scope}/{basename}"


def _artifact_scope(name: str) -> str | None:
    match = re.match(r"(?:workspace-artifacts|workspace-scenes|music-audio|references|videos|video-jobs)/([a-f0-9]{32})/", name)
    return match[1] if match else None


class ArtifactStorage:
    def __init__(self, account: str | None = None, *, service=None):
        self.account = account or os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "")
        if not re.fullmatch(r"[a-z0-9]{3,24}", self.account):
            raise ValueError("Set AZURE_STORAGE_ACCOUNT_NAME to a valid storage account.")
        self.container = "screenshots"
        self.service = service or BlobServiceClient(
            f"https://{self.account}.blob.core.windows.net", credential=DefaultAzureCredential()
        )

    def blob(self, name: str):
        return self.service.get_blob_client(self.container, validate_blob_name(name))

    def assert_workspace_active(self, scope: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", scope):
            raise ValueError("Invalid workspace scope.")
        try:
            self.blob(f"workspace-scenes/{scope}/deleted.json").get_blob_properties()
        except ResourceNotFoundError:
            return
        raise WorkspaceDeletedError("This workspace was deleted; refusing to recreate its artifacts.")

    def _write_blob(self, name: str, data, **options):
        scope = _artifact_scope(name)
        if scope:
            self.assert_workspace_active(scope)
        blob = self.blob(name)
        result = blob.upload_blob(data, **options)
        if scope:
            try:
                self.assert_workspace_active(scope)
            except WorkspaceDeletedError:
                try:
                    blob.delete_blob(etag=result["etag"], match_condition=MatchConditions.IfNotModified, delete_snapshots="include")
                except ResourceNotFoundError:
                    pass
                raise
        return result

    def upload(self, name: str, data, media_type: str, *, download_name: str | None = None):
        disposition = None
        if download_name:
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", download_name):
                raise ValueError("Invalid download filename.")
            disposition = f'attachment; filename="{download_name}"'
        return self._write_blob(
            name, data, overwrite=True,
            content_settings=ContentSettings(content_type=media_type, content_disposition=disposition),
        )

    def upload_file(self, name: str, path: Path | str, media_type: str):
        with open(path, "rb") as source:
            return self.upload(name, source, media_type)

    def download(self, name: str, path: Path, *, max_bytes: int = 200 * 1024 * 1024):
        scope = _artifact_scope(name)
        if scope:
            self.assert_workspace_active(scope)
        stream = self.blob(name).download_blob()
        if stream.size > max_bytes:
            raise ValueError("Artifact exceeds the size limit.")
        size = 0
        try:
            with path.open("wb") as target:
                for chunk in stream.chunks():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Artifact exceeds the size limit.")
                    target.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def read_json(self, name: str) -> tuple[dict, str]:
        scope = _artifact_scope(name)
        if scope:
            self.assert_workspace_active(scope)
        stream = self.blob(name).download_blob()
        if stream.size > 256 * 1024:
            raise ValueError("Manifest exceeds the size limit.")
        value = json.loads(stream.readall())
        if not isinstance(value, dict):
            raise ValueError("Invalid manifest.")
        return value, stream.properties.etag

    def write_json(self, name: str, value: dict, *, etag: str | None = None) -> str:
        payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > 256 * 1024:
            raise ValueError("Manifest exceeds the size limit.")
        conditions = {"etag": etag, "match_condition": MatchConditions.IfNotModified} if etag else {}
        result = self._write_blob(
            name, payload, overwrite=etag is not None,
            content_settings=ContentSettings(content_type="application/json", cache_control="no-store"),
            **conditions,
        )
        return result["etag"]

    def signed_url(self, name: str, *, hours: int = 24) -> str:
        validate_blob_name(name)
        if not 1 <= hours <= 48:
            raise ValueError("SAS lifetime must be between 1 and 48 hours.")
        start = datetime.now(timezone.utc) - timedelta(minutes=5)
        expiry = datetime.now(timezone.utc) + timedelta(hours=hours)
        delegation = self.service.get_user_delegation_key(start, expiry)
        token = generate_blob_sas(
            account_name=self.account, container_name=self.container, blob_name=name,
            user_delegation_key=delegation, permission=BlobSasPermissions(read=True),
            start=start, expiry=expiry, protocol="https",
        )
        return f"https://{self.account}.blob.core.windows.net/{self.container}/{name}?{token}"