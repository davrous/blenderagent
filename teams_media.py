"""Bounded Teams reference ingestion and explicit, channel-owned video controls."""

from __future__ import annotations

import asyncio
import base64
import binascii
import math
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import urlsplit

MAX_BYTES = 200 * 1024 * 1024
MAX_INLINE_BYTES = 8 * 1024 * 1024
DOWNLOAD_SECONDS = 60
MEDIA_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/mp4": "mp4"}
FILE_INFO = "application/vnd.microsoft.teams.file.download.info"
ID_PATTERN = r"[a-f0-9]{32}"


class MediaError(ValueError):
    """An intentionally credential-free message suitable for the channel."""


def field(value, name, alias=None):
    if isinstance(value, dict):
        return value.get(name, value.get(alias))
    return getattr(value, name, None)


def authenticated(context):
    return getattr(getattr(context, "identity", None), "is_authenticated", False) is True


def attachment_specs(attachments):
    if not isinstance(attachments, list) or not attachments:
        raise MediaError("Send between one and four reference attachments (200 MiB maximum each).")
    attachments = [attachment for attachment in attachments if not (
        field(attachment, "content_type", "contentType") == "text/html"
        and not field(attachment, "name")
        and not field(attachment, "content_url", "contentUrl")
        and isinstance(field(attachment, "content"), str)
    )]
    if len(attachments) > 4:
        raise MediaError("Send between one and four reference attachments (200 MiB maximum each).")
    specs = []
    for attachment in attachments:
        media_type = field(attachment, "content_type", "contentType")
        name = field(attachment, "name")
        url = field(attachment, "content_url", "contentUrl")
        if media_type == FILE_INFO:
            content = field(attachment, "content")
            if not isinstance(content, dict):
                raise MediaError("Re-upload the file in Teams; its download information is missing.")
            url = content.get("downloadUrl")
            suffix = Path(name).suffix.lower().lstrip(".") if isinstance(name, str) else ""
            media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                          "webp": "image/webp", "mp4": "video/mp4"}.get(suffix)
        if not isinstance(media_type, str) or media_type not in MEDIA_TYPES:
            raise MediaError("Use PNG, JPEG, WebP or MP4 references. Other attachment types are not supported.")
        if name is not None:
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _().-]{0,127}", name):
                raise MediaError("Rename the reference using a short filename without paths or special characters.")
            suffix = Path(name).suffix.lower().lstrip(".")
            if suffix not in ({"jpg", "jpeg"} if media_type == "image/jpeg" else {MEDIA_TYPES[media_type]}):
                raise MediaError("The reference filename must match its PNG, JPEG, WebP or MP4 type.")
        if not isinstance(url, str) or not url:
            raise MediaError("Re-upload the reference in Teams; no downloadable attachment was supplied.")
        if url.startswith("data:"):
            prefix = f"data:{media_type};base64,"
            if media_type == "video/mp4" or not url.startswith(prefix) or len(url) > MAX_INLINE_BYTES * 4 // 3 + 100:
                raise MediaError("Inline images must be PNG, JPEG or WebP and at most 8 MiB; upload larger files in Teams.")
        elif len(url) > 16384:
            raise MediaError("The attachment download address is invalid. Re-upload the file in Teams.")
        specs.append((media_type, url))
    return specs


def connector_attachment(context, url):
    if not authenticated(context):
        return False
    try:
        service = urlsplit(getattr(context.activity, "service_url", "") or "")
        target = urlsplit(url)
        host = service.hostname or ""
        trusted = host in {"smba.trafficmanager.net", "smba.infra.teams.microsoft.com"} or host.endswith(".botframework.com")
        route = re.escape(service.path.rstrip("/")) + r"/v3/attachments/[A-Za-z0-9_-]{1,256}/views/[A-Za-z0-9_-]{1,64}"
        return bool(trusted and service.scheme == target.scheme == "https"
                    and target.hostname == host and service.port in (None, 443) and target.port in (None, 443)
                    and not service.username and not service.password and not service.query and not service.fragment
                    and not target.username and not target.password and not target.query and not target.fragment
                    and re.fullmatch(route, target.path))
    except ValueError:
        return False


async def _copy_response(session, url, path):
    import aiohttp

    async with session.get(url, allow_redirects=False, auto_decompress=False,
                           timeout=aiohttp.ClientTimeout(total=DOWNLOAD_SECONDS, connect=15)) as response:
        if response.status in (401, 403):
            raise MediaError("Teams could not authorize this download. Re-upload the file using Teams file attachment, not a protected link.")
        if response.status != 200:
            raise MediaError("The attachment could not be downloaded. Redirects are not followed; re-upload the file in Teams.")
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise MediaError("Compressed HTTP attachments are unsupported. Re-upload the original media file.")
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_BYTES):
            raise MediaError("Each reference must contain between one byte and 200 MiB.")
        size = 0
        with path.open("wb") as target:
            async for chunk in response.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise MediaError("Each reference must be at most 200 MiB.")
                target.write(chunk)
        if not size:
            raise MediaError("The reference attachment is empty.")


async def download_attachment(context, media_type, url, path):
    import aiohttp
    from wavespeed_client import public_https

    if url.startswith("data:"):
        try:
            data = base64.b64decode(url.split(",", 1)[1], validate=True)
        except (ValueError, binascii.Error):
            raise MediaError("The inline reference image is not valid base64.") from None
        if not 0 < len(data) <= MAX_INLINE_BYTES:
            raise MediaError("Inline reference images must be between one byte and 8 MiB.")
        path.write_bytes(data)
        return
    try:
        async with asyncio.timeout(DOWNLOAD_SECONDS):
            await asyncio.to_thread(public_https, url)
            if connector_attachment(context, url):
                adapter = context.adapter
                factory = context.turn_state.get(getattr(adapter, "CHANNEL_SERVICE_FACTORY_KEY", ""))
                audience = context.turn_state.get(getattr(adapter, "OAUTH_SCOPE_KEY", ""))
                if factory is None or not audience:
                    raise MediaError("Connector authentication is unavailable. Re-upload the file as a Teams file attachment.")
                client = await factory.create_connector_client(
                    context, context.identity, context.activity.service_url, audience,
                    context.identity.get_token_scope(), False,
                )
                try:
                    session = getattr(client, "client", None)
                    if session is None:
                        raise MediaError("This connector cannot safely download protected media. Use a Teams file attachment.")
                    await _copy_response(session, url, path)
                finally:
                    await client.close()
            else:
                async with aiohttp.ClientSession(trust_env=False) as session:
                    await _copy_response(session, url, path)
    except MediaError:
        raise
    except Exception:
        raise MediaError("Could not safely download the attachment within 60 seconds. Use a public HTTPS Teams attachment and retry.") from None


async def store_references(context, specs, scope, current):
    from media_analysis import inspect_media
    from video_jobs import get_video_jobs

    if not authenticated(context) or not isinstance(scope, str) or not re.fullmatch(ID_PATTERN, scope):
        raise MediaError("Reference uploads require an authenticated Teams conversation.")
    try:
        with tempfile.TemporaryDirectory(prefix="teams-reference-") as temporary:
            validated = []
            for index, (media_type, url) in enumerate(specs):
                if not current():
                    raise MediaError("This upload was superseded by /clear. Send the reference again.")
                path = Path(temporary) / f"reference-{index}.{MEDIA_TYPES[media_type]}"
                await download_attachment(context, media_type, url, path)
                metadata = await asyncio.to_thread(inspect_media, path)
                if metadata["media_type"] != media_type:
                    raise MediaError("The decoded reference does not match its attachment type.")
                validated.append((path, metadata))
            references = []
            for path, metadata in validated:
                if not current():
                    raise MediaError("This upload was superseded by /clear. Send the reference again.")
                reference_id = f"references/{scope}/{uuid.uuid4().hex}.{MEDIA_TYPES[metadata['media_type']]}"
                await asyncio.to_thread(get_video_jobs().storage.upload_file, reference_id, path, metadata["media_type"])
                references.append(reference_id)
            return references
    except MediaError:
        raise
    except Exception:
        raise MediaError("Reference validation or storage failed. Use a decodable PNG/JPEG/WebP image (max 16 MP), or a 4-30 second MP4 (max 1080p). The host needs ffmpeg, ffprobe and Blob access.") from None


def validate_action(value, scope):
    if not isinstance(value, dict) or value.get("type") != "blenderVideoAction":
        raise MediaError("Invalid video action. Use the buttons on the video job card.")
    if not isinstance(scope, str) or not re.fullmatch(ID_PATTERN, scope) or value.get("scope") != scope:
        raise MediaError("This video card belongs to an older or different scene. It cannot control jobs after /clear.")
    job_id = value.get("job_id")
    action = value.get("action")
    if not isinstance(job_id, str) or not re.fullmatch(ID_PATTERN, job_id) or action not in ("approve", "status", "cancel"):
        raise MediaError("Invalid video job or action. Use the original video card.")
    fields = {"type", "scope", "job_id", "action"}
    if action == "approve":
        fields |= {"prompt", "resolution", "generate_audio", "consent", "estimate_usd"}
        prompt = value.get("prompt")
        price = value.get("estimate_usd")
        if (not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000
                or value.get("resolution") != "720p" or value.get("generate_audio") is not False
                or value.get("consent") != "true" or type(price) not in (int, float)
                or not math.isfinite(price) or price < 0):
            raise MediaError("Enter a finishing prompt and explicitly consent to paid WaveSpeed processing at 720p without audio.")
    if set(value) - fields:
        raise MediaError("Unexpected video action fields. Use the original video card.")
    return {"type": action, "job_id": job_id, **({"prompt": value["prompt"].strip(),
            "resolution": "720p", "generate_audio": False} if action == "approve" else {})}


def register_notifier(jobs, job_id, notifier):
    if notifier is not None:
        jobs._notifications[job_id] = notifier


async def run_video_action(context, value, scope, notifier, current):
    if not authenticated(context):
        raise MediaError("Video controls require an authenticated Teams conversation.")
    action = validate_action(value, scope)

    def control():
        from media_control import control_action
        from video_jobs import get_video_jobs

        jobs = get_video_jobs()
        document, _etag = jobs.repo.read(scope, action["job_id"])
        if not current():
            raise MediaError("This video action was superseded by /clear.")
        if action["type"] == "approve":
            description = jobs.describe(document)
            if document["state"] != "awaiting_seedance_approval":
                if not current():
                    raise MediaError("This video action was superseded by /clear.")
                register_notifier(jobs, action["job_id"], notifier)
                return jobs.status(scope, action["job_id"])
            if not description.get("seedance_enabled") or description.get("estimate_usd") != value["estimate_usd"]:
                raise MediaError("Seedance is unavailable or its estimate changed. Refresh Status and review approval again.")
        register_notifier(jobs, action["job_id"], notifier)
        if not current():
            raise MediaError("This video action was superseded by /clear.")
        return control_action(scope, action)

    try:
        return await asyncio.to_thread(control)
    except MediaError:
        raise
    except Exception:
        raise MediaError("The video action could not be completed for this scene. Refresh Status; check Blob access and Seedance configuration. A paid submission is never retried automatically.") from None


def video_attachments(value, scope, attachment_type):
    job_id = value.get("id")
    if not isinstance(job_id, str) or not re.fullmatch(ID_PATTERN, job_id):
        raise MediaError("The video job descriptor is invalid.")
    state = value.get("state", "unknown")
    if state not in {"queued", "rendering", "encoding", "awaiting_seedance_approval", "wavespeed_uploading",
                     "wavespeed_submitting", "wavespeed_processing", "completed", "failed", "cancelled", "submission_unknown"}:
        raise MediaError("The video job state is invalid.")
    base = {"type": "blenderVideoAction", "scope": scope, "job_id": job_id}
    progress = value.get("progress", 0)
    if type(progress) not in (int, float) or not math.isfinite(progress):
        progress = 0
    body = [{"type": "TextBlock", "text": "Video job", "weight": "Bolder", "wrap": True},
            {"type": "TextBlock", "text": f"{state} | {max(0, min(100, progress)):g}%", "wrap": True},
            {"type": "TextBlock", "text": f"Job ID: {job_id}", "wrap": True},
            {"type": "TextBlock", "text": "Status refreshes links and resumes recoverable work after idle. Background delivery requires the host to remain running.", "wrap": True}]
    actions = [{"type": "Action.Submit", "title": "Status", "associatedInputs": "none", "data": {**base, "action": "status"}}]
    attachments = []
    url = value.get("output_url") or value.get("preview_url")
    poster = value.get("poster_url")
    def safe_artifact(address, suffix):
        if not isinstance(address, str) or len(address) > 16384:
            return False
        try:
            parsed = urlsplit(address)
            return bool(parsed.scheme == "https" and parsed.hostname and parsed.hostname.endswith(".blob.core.windows.net")
                        and not parsed.username and not parsed.password and parsed.port in (None, 443)
                        and parsed.path.startswith(f"/screenshots/videos/{scope}/{job_id}/")
                        and parsed.path.endswith(suffix))
        except ValueError:
            return False
    if safe_artifact(poster, ".png"):
        body.append({"type": "Image", "url": poster, "altText": "Video preview", "size": "Large"})
    if safe_artifact(url, ".mp4"):
        body.append({"type": "Media", "sources": [{"mimeType": "video/mp4", "url": url}],
                     "fallback": {"type": "TextBlock", "text": "Use Download MP4 to view the video.", "wrap": True}})
        actions.append({"type": "Action.OpenUrl", "title": "Download MP4", "url": url})
        attachments.append(attachment_type(content_type="video/mp4", content_url=url, name="video.mp4"))
    if state == "awaiting_seedance_approval":
        price = value.get("estimate_usd")
        if value.get("seedance_enabled") is True and type(price) in (int, float) and math.isfinite(price) and price >= 0:
            body.append({"type": "TextBlock", "text": f"Optional Seedance finish: estimated ${price:.2f} USD. Your preview and prompt will be sent to third-party WaveSpeed. 720p, audio off.", "wrap": True})
            actions.append({"type": "Action.ShowCard", "title": "Review paid Seedance finish", "card": {
                "type": "AdaptiveCard", "version": "1.3", "body": [
                    {"type": "Input.Text", "id": "prompt", "label": "Finishing prompt", "isMultiline": True, "isRequired": True, "maxLength": 8000},
                    {"type": "Input.Toggle", "id": "consent", "title": f"I approve third-party WaveSpeed processing and the estimated ${price:.2f} USD charge (720p, audio off).",
                     "value": "false", "valueOn": "true", "valueOff": "false", "isRequired": True}],
                "actions": [{"type": "Action.Submit", "title": "Approve paid finish", "data": {
                    **base, "action": "approve", "resolution": "720p", "generate_audio": False, "estimate_usd": price}}]}})
        else:
            body.append({"type": "TextBlock", "text": "Seedance finishing is not configured on this host. The preview remains available.", "wrap": True})
    if state in {"queued", "rendering", "encoding", "awaiting_seedance_approval", "wavespeed_uploading", "wavespeed_processing"}:
        actions.append({"type": "Action.Submit", "title": "Cancel", "associatedInputs": "none", "data": {**base, "action": "cancel"}})
    if state in {"failed", "submission_unknown"}:
        body.append({"type": "TextBlock", "text": "Processing failed. Check diagnostics and refresh Status." if state == "failed" else
                     "Submission outcome is unknown. Reconcile in WaveSpeed before creating another paid request.", "wrap": True})
    card = attachment_type(content_type="application/vnd.microsoft.card.adaptive", content={
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.3",
        "body": body, "actions": actions})
    return [card, *attachments]