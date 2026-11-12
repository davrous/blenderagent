"""Read-only Teams scene widgets backed by trusted GLB exports."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import html
import json
import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from teams_media import authenticated, field

logger = logging.getLogger("blender_agent.widgets")
STATE_KEY = "blender_scene_widgets"
MAX_WIDGETS = 32
MAX_GLB_BYTES = 200 * 1024 * 1024
INVOKE_NAME = "htmlwidget/calltool"
_TEMPLATE = Path(__file__).parent / "webchat" / "client" / "widget" / "widget.html"
SCENE_PREVIEW_INSTRUCTIONS = (
    "For THIS Teams turn, the default visual reply is an interactive Babylon.js 3D scene, "
    "NOT a screenshot or a rendered image. This overrides the normal final-screenshot rule. "
    "Only build or modify the scene when the user asks for changes, then briefly describe the result. "
    "After successful scene edits, the runtime automatically exports the finished scene as GLB and publishes its HTML widget; "
    "do not ask the user to request a GLB, click Load scene, or approve a preview. "
    "Do not call get_viewport_screenshot, render_preview or render_final just to show a scene edit. "
    "If the user explicitly asks for an image, screenshot or video, use the corresponding media tools "
    "and return that media instead of a 3D widget, even if you also edit the scene. "
    "For model/texture listings or questions without scene changes, return only the gallery or answer; "
    "do not call screenshot, export or show_3d_scene tools to illustrate those replies. "
    "For read-only execute_blender_code queries, set modifies_scene=False. "
    "If the user explicitly asks to see the 3D scene (for example 'show me the 3D scene'), "
    "call show_3d_scene even when no scene changes are needed. "
    "Do not call export_scene_as_glb_for_download unless the user specifically asks for an export: "
    "the automatic preview already provides a downloadable GLB. Never fabricate widget HTML or URLs. "
    "The widget renders an exported snapshot interactively, not a live connection to Blender; "
    "each subsequent scene edit publishes a new snapshot unless another output was requested."
)


class WidgetError(ValueError):
    """A credential-free error suitable for display in Teams."""


@dataclass(frozen=True)
class SceneExport:
    id: str
    blob: str
    account: str
    size: int
    etag: str


exports: contextvars.ContextVar[list[SceneExport] | None] = contextvars.ContextVar(
    "teams_widget_exports", default=None,
)


def record_export(blob: str, account: str, size: int, etag: str) -> None:
    """Called only by the upload helper, never from model-generated URLs."""
    captured = exports.get()
    if captured is not None:
        captured.append(SceneExport(uuid.uuid4().hex, blob, account, size, etag))


@dataclass(frozen=True)
class WidgetConfig:
    assets: str
    asset_origin: str
    storage_origin: str


def configuration() -> WidgetConfig | None:
    assets = os.environ.get("TEAMS_WIDGET_ASSET_BASE_URL", "").strip().rstrip("/")
    if not assets:
        return None
    parsed = urlsplit(assets)
    if (
        parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.query or parsed.fragment
        or parsed.port not in (None, 443) or parsed.hostname in ("localhost", "127.0.0.1")
    ):
        raise WidgetError("TEAMS_WIDGET_ASSET_BASE_URL must be a public HTTPS asset directory.")
    account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "")
    if not re.fullmatch(r"[a-z0-9]{3,24}", account):
        raise WidgetError("Scene widgets require a valid AZURE_STORAGE_ACCOUNT_NAME.")
    return WidgetConfig(assets, f"https://{parsed.netloc}", f"https://{account}.blob.core.windows.net")


def context_binding(context) -> str:
    """Bind the SDK-authenticated activity to its tenant, bot and thread."""
    activity = context.activity
    channel = getattr(activity.channel_id, "channel", activity.channel_id)
    tenant = field(field(getattr(activity, "channel_data", None), "tenant"), "id")
    tenant = tenant or field(getattr(activity, "conversation", None), "tenant_id")
    bot = field(getattr(activity, "recipient", None), "id")
    conversation = field(getattr(activity, "conversation", None), "id")
    if not authenticated(context) or channel != "msteams" or not all(
        isinstance(value, str) and 0 < len(value) <= 2048 for value in (tenant, bot, conversation)
    ):
        raise WidgetError("The widget requires an authenticated Teams conversation and tenant.")
    return hashlib.sha256(json.dumps([tenant, bot, conversation], separators=(",", ":")).encode()).hexdigest()


def remember(export: SceneExport, scope: str, binding: str, generation: int, records: list[dict]) -> list[dict]:
    return [*records[-(MAX_WIDGETS - 1):], {
        **asdict(export), "scope": scope, "binding": binding, "generation": generation, "version": 1,
    }]


def markdown(widget_id: str, config: WidgetConfig) -> str:
    bootstrap = json.dumps({
        "id": widget_id,
        "storageOrigin": config.storage_origin,
        "environmentUrl": f"{config.assets}/studio_small_09_1k.hdr",
    }, separators=(",", ":")).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    document = _TEMPLATE.read_text(encoding="utf-8").replace("__CONFIG__", bootstrap)
    document = document.replace("__ASSET_BASE__", html.escape(config.assets, quote=True))
    payload = {
        "type": "widget/mcp-ui", "name": "Blender scene",
        "description": "Automatically loads this exported Blender scene in 3D. Read-only snapshot.",
        "domain": config.asset_origin, "correlationId": widget_id, "html": document,
        "securityPolicy": {
            "connectDomains": [config.asset_origin, config.storage_origin],
            "resourceDomains": [config.asset_origin],
            "frameDomains": [], "baseUriDomains": [],
        },
    }
    return "```html-widget\n" + json.dumps(payload, separators=(",", ":")) + "\n```"


def _result(text: str, structured: dict | None = None, *, error: bool = False) -> dict:
    return {
        "responseType": "htmlwidget/calltoolresult",
        "callToolResult": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured or {}, "isError": error,
        },
    }


def _load_snapshot(record: dict, config: WidgetConfig) -> dict:
    from artifact_storage import ArtifactStorage, validate_blob_name

    account = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "")
    if record.get("account") != account or record.get("version") != 1:
        raise WidgetError("This snapshot uses an unavailable storage account or widget version.")
    name = record.get("blob")
    if not isinstance(name, str) or not name.startswith("scenes/") or not name.endswith(".glb"):
        raise WidgetError("The snapshot reference is invalid. Export the scene again.")
    validate_blob_name(name)
    storage = ArtifactStorage(account)
    properties = storage.blob(name).get_blob_properties()
    if not 0 < properties.size <= MAX_GLB_BYTES or properties.size != record.get("size"):
        raise WidgetError("This snapshot is empty, changed or larger than 200 MiB. Export a smaller scene.")
    if properties.etag != record.get("etag"):
        raise WidgetError("This snapshot has changed. Export the scene again.")
    url = storage.signed_url(name, hours=1)
    if not url.startswith(config.storage_origin + "/screenshots/scenes/"):
        raise WidgetError("The snapshot storage address is invalid.")
    return {
        "version": 1, "id": record["id"], "url": url, "size": properties.size,
        "filename": "blender_scene.glb",
        "expiresAt": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }


async def call_tool(context, records: list[dict], scope: str | None, generation: int, current) -> dict:
    try:
        config = configuration()
        if config is None:
            raise WidgetError("Scene widgets are disabled on this agent.")
        binding = context_binding(context)
        value = getattr(context.activity, "value", None)
        name, arguments = field(value, "name"), field(value, "arguments")
        widget_id = arguments.get("id") if isinstance(arguments, dict) else None
        if name not in ("scene_widget_ping", "load_scene_snapshot"):
            raise WidgetError("This widget only supports checking its connection and reading an exported scene.")
        if not isinstance(widget_id, str) or not re.fullmatch(r"[a-f0-9]{32}", widget_id):
            raise WidgetError("The widget identifier is invalid.")
        record = next((item for item in records if item.get("id") == widget_id), None)
        if (
            record is None or record.get("binding") != binding or record.get("scope") != scope
            or record.get("generation") != generation or not current()
        ):
            raise WidgetError("This snapshot is unavailable in this conversation or was cleared. Export the scene again.")
        if name == "scene_widget_ping":
            return _result("Widget connection ready.", {"version": 1, "id": widget_id})
        async with asyncio.timeout(12):
            snapshot = await asyncio.to_thread(_load_snapshot, record, config)
        if not current():
            raise WidgetError("The scene was cleared while loading. Export the scene again.")
        return _result("Snapshot ready.", snapshot)
    except WidgetError as error:
        logger.warning("Scene widget request rejected: %s", error)
        return _result(str(error), error=True)
    except Exception as error:
        # Storage exceptions can embed SAS addresses; never log their repr.
        logger.warning("Scene widget request failed (%s)", type(error).__name__)
        return _result("The snapshot could not be loaded. Retry after the agent wakes up, or export the scene again.", error=True)
