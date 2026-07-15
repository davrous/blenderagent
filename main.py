"""
Blender Scene Agent - An AI agent that creates and manipulates 3D scenes in Blender.
Communicates with a headless Blender instance via TCP socket (BlenderMCP protocol).
Uses Microsoft Agent Framework with Azure AI Foundry.
"""

import asyncio
import base64
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Annotated

import httpx
from dotenv import load_dotenv

load_dotenv(override=True)

from agent_framework import (
    Agent,
    AgentMiddleware,
    AgentContext,
    AgentResponse,
    AgentResponseUpdate,
    Content,
    ResponseStream,
    tool,
)
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer

from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.storage.blob import BlobServiceClient, BlobSasPermissions, ContentSettings, generate_blob_sas

from blender_connection import get_blender_connection, close_blender_connection, is_blender_socket_ready
from scene_manager import SceneManager

# Module-level reference so _do_render can recover the scene after a Blender crash.
_scene_manager: SceneManager | None = None

logger = logging.getLogger("blender_agent")
logger.setLevel(logging.DEBUG)
logger.propagate = False
if not logger.handlers:
    _fmt = logging.Formatter("%(levelname)s: %(name)s: %(message)s")
    # DEBUG / INFO → stdout (so they don't appear as errors in console)
    _stdout_handler = logging.StreamHandler(sys.stdout)
    _stdout_handler.setLevel(logging.DEBUG)
    _stdout_handler.addFilter(lambda r: r.levelno < logging.WARNING)
    _stdout_handler.setFormatter(_fmt)
    # WARNING+ → stderr
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setLevel(logging.WARNING)
    _stderr_handler.setFormatter(_fmt)
    logger.addHandler(_stdout_handler)
    logger.addHandler(_stderr_handler)

    # Persistent file log to $HOME/logs/ — survives idle deprovision/reprovision
    # so we can troubleshoot session-restore failures after the fact.
    _persistent_log_dir = os.path.join(os.path.expanduser("~"), "logs")
    os.makedirs(_persistent_log_dir, exist_ok=True)
    _persistent_log_path = os.path.join(_persistent_log_dir, "agent.log")
    from logging.handlers import RotatingFileHandler
    _file_handler = RotatingFileHandler(
        _persistent_log_path, maxBytes=5 * 1024 * 1024, backupCount=3
    )
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    ))
    logger.addHandler(_file_handler)
    logger.info("Persistent log file: %s", _persistent_log_path)

# Suppress harmless "Failed to detach context" errors from the framework's
# internal OpenTelemetry instrumentation (async generator context mismatch).
logging.getLogger("opentelemetry.context").setLevel(logging.CRITICAL)

# Azure AI Foundry configuration
PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")

# Azure Blob Storage configuration.
# NOTE: no default value on purpose. If AZURE_STORAGE_ACCOUNT_NAME is unset
# (e.g. agent.yaml template not substituted by the deployment pipeline) we want
# to fail loudly rather than silently target somebody else's storage account.
AZURE_STORAGE_ACCOUNT_NAME = os.environ.get("AZURE_STORAGE_ACCOUNT_NAME")
BLOB_CONTAINER_NAME = "screenshots"

# Persistent temp directory under $HOME — survives idle deprovision/reprovision.
# Used for screenshots, renders, and other transient files. Useful for
# post-mortem debugging (the most recent render is still on disk after restore).
_PERSISTENT_TMP = os.path.join(os.path.expanduser("~"), "tmp")
os.makedirs(_PERSISTENT_TMP, exist_ok=True)

# ── Asset libraries ─────────────────────────────────────────────────────────
# Microsoft Office / PowerPoint 3D-model media service — the same public
# endpoint the PowerPoint "3D Models" picker uses. Returns thumbnail images +
# GLB download links, which the web client renders as a selectable gallery.
MODEL_SEARCH_URL = os.environ.get(
    "MODEL_SEARCH_URL",
    "https://hubble.officeapps.live.com/mediasvc/api/media/search?v=1&lang=en-us",
)
MODEL_SEARCH_PAGE_SIZE = int(os.environ.get("MODEL_SEARCH_PAGE_SIZE", "5"))

# Poly Haven public asset API — used ONLY for free PBR surface textures now.
POLYHAVEN_API = os.environ.get("POLYHAVEN_API", "https://api.polyhaven.com").rstrip("/")
TEXTURE_SEARCH_PAGE_SIZE = int(os.environ.get("TEXTURE_SEARCH_PAGE_SIZE", "6"))
TEXTURE_DEFAULT_RESOLUTION = os.environ.get("TEXTURE_DEFAULT_RESOLUTION", "2k")
_HTTP_HEADERS = {"User-Agent": "BlenderSceneAgent/1.0"}

logger.info(
    "Storage config: AZURE_STORAGE_ACCOUNT_NAME=%r (env_set=%s)",
    AZURE_STORAGE_ACCOUNT_NAME,
    "AZURE_STORAGE_ACCOUNT_NAME" in os.environ,
)
if not AZURE_STORAGE_ACCOUNT_NAME or AZURE_STORAGE_ACCOUNT_NAME.startswith("{{"):
    logger.error(
        "AZURE_STORAGE_ACCOUNT_NAME is not set or is an unsubstituted template (%r). "
        "Blob uploads WILL fail. Check agent.yaml env var substitution.",
        AZURE_STORAGE_ACCOUNT_NAME,
    )


def _log_storage_principal_once() -> None:
    """One-shot diagnostic: log oid/appid/tid of the MSI principal used for storage auth.

    This decodes the JWT payload (no signature verification — diagnostic only) so that
    the runtime managed-identity principal is visible in container logs. Required for
    diagnosing 'AuthorizationFailure' (HTTP 403) errors when the runtime identity
    differs from the one previously granted RBAC on the storage account.
    """
    if getattr(_log_storage_principal_once, "_done", False):
        return
    _log_storage_principal_once._done = True  # type: ignore[attr-defined]
    try:
        cred = SyncDefaultAzureCredential()
        token = cred.get_token("https://storage.azure.com/.default").token
        # JWT = header.payload.signature
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        logger.info(
            "Storage MSI principal: oid=%s appid=%s tid=%s xms_mirid=%s idp=%s",
            payload.get("oid"),
            payload.get("appid"),
            payload.get("tid"),
            payload.get("xms_mirid"),
            payload.get("idp") or payload.get("idtyp"),
        )
    except Exception as e:  # pragma: no cover - diagnostic best-effort
        logger.warning("Could not log storage MSI principal: %s", e)


def upload_image_to_blob(image_bytes: bytes, blob_name: str) -> str:
    """Upload image bytes to Azure Blob Storage and return the public URL."""
    logger.info("Uploading image to blob: %s (%d bytes)", blob_name, len(image_bytes))
    _log_storage_principal_once()
    account_url = f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
    credential = SyncDefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url, credential=credential)
    container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

    # Container is expected to be pre-created. We deliberately do NOT call
    # create_container() here: under least-privilege RBAC ('Storage Blob Data
    # Contributor') a 403 on get_container_properties masks the real upload
    # error and is misleading in logs.
    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        image_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type="image/png"),
    )

    # Generate a user-delegation SAS token (1-hour expiry)
    from datetime import timedelta
    sas_start = datetime.now(timezone.utc)
    sas_expiry = sas_start + timedelta(hours=1)
    user_delegation_key = blob_service_client.get_user_delegation_key(
        key_start_time=sas_start,
        key_expiry_time=sas_expiry,
    )
    sas_token = generate_blob_sas(
        account_name=AZURE_STORAGE_ACCOUNT_NAME,
        container_name=BLOB_CONTAINER_NAME,
        blob_name=blob_name,
        user_delegation_key=user_delegation_key,
        permission=BlobSasPermissions(read=True),
        expiry=sas_expiry,
    )
    return f"{account_url}/{BLOB_CONTAINER_NAME}/{blob_name}?{sas_token}"


def upload_blend_to_blob(local_path: str, blob_name: str) -> str:
    """Upload a .blend file to Azure Blob Storage and return a download URL."""
    file_size = os.path.getsize(local_path)
    logger.info("Uploading .blend to blob: %s (%d bytes)", blob_name, file_size)
    account_url = f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
    credential = SyncDefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url, credential=credential)
    container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

    # Container is expected to be pre-created (see note in upload_image_to_blob).
    blob_client = container_client.get_blob_client(blob_name)
    with open(local_path, "rb") as f:
        blob_client.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="application/x-blender",
                content_disposition="attachment; filename=blender_scene.blend",
            ),
        )

    # Generate a user-delegation SAS token (1-hour expiry)
    from datetime import timedelta
    sas_start = datetime.now(timezone.utc)
    sas_expiry = sas_start + timedelta(hours=1)
    user_delegation_key = blob_service_client.get_user_delegation_key(
        key_start_time=sas_start,
        key_expiry_time=sas_expiry,
    )
    sas_token = generate_blob_sas(
        account_name=AZURE_STORAGE_ACCOUNT_NAME,
        container_name=BLOB_CONTAINER_NAME,
        blob_name=blob_name,
        user_delegation_key=user_delegation_key,
        permission=BlobSasPermissions(read=True),
        expiry=sas_expiry,
    )
    return f"{account_url}/{BLOB_CONTAINER_NAME}/{blob_name}?{sas_token}"


def upload_glb_to_blob(local_path: str, blob_name: str) -> str:
    """Upload a .glb file to Azure Blob Storage and return a download URL."""
    file_size = os.path.getsize(local_path)
    logger.info("Uploading .glb to blob: %s (%d bytes)", blob_name, file_size)
    account_url = f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
    credential = SyncDefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url, credential=credential)
    container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

    # Container is expected to be pre-created (see note in upload_image_to_blob).
    blob_client = container_client.get_blob_client(blob_name)
    with open(local_path, "rb") as f:
        blob_client.upload_blob(
            f,
            overwrite=True,
            content_settings=ContentSettings(
                content_type="model/gltf-binary",
                content_disposition="attachment; filename=blender_scene.glb",
            ),
        )

    # Generate a user-delegation SAS token (1-hour expiry)
    from datetime import timedelta
    sas_start = datetime.now(timezone.utc)
    sas_expiry = sas_start + timedelta(hours=1)
    user_delegation_key = blob_service_client.get_user_delegation_key(
        key_start_time=sas_start,
        key_expiry_time=sas_expiry,
    )
    sas_token = generate_blob_sas(
        account_name=AZURE_STORAGE_ACCOUNT_NAME,
        container_name=BLOB_CONTAINER_NAME,
        blob_name=blob_name,
        user_delegation_key=user_delegation_key,
        permission=BlobSasPermissions(read=True),
        expiry=sas_expiry,
    )
    return f"{account_url}/{BLOB_CONTAINER_NAME}/{blob_name}?{sas_token}"


MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1")

# ──────────────────────────────────────────────
# Resilience knobs (overridable via env)
# ──────────────────────────────────────────────
# Time without any streaming chunk before we emit a "still working" heartbeat
# to keep the client connection alive and reassure the user.
HEARTBEAT_INTERVAL_SECONDS = float(os.getenv("AGENT_HEARTBEAT_SECONDS", "30"))
# Hard wall-clock cap for a single agent turn. Aborts cleanly with a friendly
# message rather than letting the request hang indefinitely.
TURN_TIMEOUT_SECONDS = float(os.getenv("AGENT_TURN_TIMEOUT_SECONDS", "180"))
# User-facing message when the upstream model fails or the turn times out.
_FRIENDLY_MODEL_ERROR_TEXT = (
    "\n\n⚠️ The model service hit a transient error and could not finish this "
    "response. Your scene has been saved — please retry your last message."
)
_FRIENDLY_TIMEOUT_TEXT = (
    "\n\n⚠️ This turn took too long and was aborted to keep the session healthy. "
    "Your scene has been saved — please retry, ideally with a simpler request."
)
# User-facing message for a corrupted conversation history: a previous turn was
# interrupted (idle recycle, timeout, or a dropped connection) after the model
# asked for a tool but before the tool's output was recorded, leaving an
# orphaned function call in the server-side session. Retrying replays the same
# orphan, so the ONLY fix is to start a fresh session — the web client detects
# this message's underlying error and resets the session automatically.
_FRIENDLY_STATE_ERROR_TEXT = (
    "\n\n⚠️ This conversation's history got out of sync (a tool call was "
    "interrupted before it finished), so the model rejected it. Your scene has "
    "been saved. Starting a fresh session — please resend your last message. "
    "(If it keeps happening, click Reset.)"
)


def _is_orphaned_tool_call_error(exc: Exception) -> bool:
    """True when the upstream 400 means the history has a function call with no output.

    The Responses API validates that every function call in the replayed history
    has a matching tool output. If a prior turn was cut off mid-tool-call, the
    orphaned call poisons the session and every subsequent turn fails with
    ``No tool output found for function call …``. This is unrecoverable by retry.
    """
    msg = str(exc).lower()
    return "no tool output found for function call" in msg



# ──────────────────────────────────────────────
# Crash detection & recovery helpers
# ──────────────────────────────────────────────

# Crash counter — circuit breaker to avoid retry storms when Blender keeps
# crashing on the same input (e.g. a problematic asset). On Foundry's ADC
# platform each agent runs in its own micro-VM bound 1:1 to a conversation,
# so a single counter is sufficient.
_MAX_CRASHES = 2
_crash_count: int = 0


def _is_blender_crash_error(err: Exception) -> bool:
    """Heuristic: True if the error indicates Blender is dead/restarted, not an app-level error."""
    msg = str(err).lower()
    return any(
        marker in msg
        for marker in (
            "connection lost",
            "connection refused",
            "not connected to blender",
            "process is not running",
            "connection closed before receiving any data",
            "broken pipe",
        )
    )


def _recover_blender_scene(label: str) -> bool:
    """Wait for Blender to come back and reload the persisted scene.

    Returns True on successful recovery. Caller should surface a friendly
    error to the LLM/user on False.
    """
    global _crash_count
    logger.error("BLENDER_CRASH_DETECTED in %s — attempting recovery", label)

    if _scene_manager is None:
        logger.error("%s: scene manager not initialized — cannot recover", label)
        return False

    # Circuit breaker
    _crash_count += 1
    if _crash_count > _MAX_CRASHES:
        logger.error(
            "%s: exceeded %d crashes — circuit breaker tripped",
            label, _MAX_CRASHES,
        )
        return False

    if not _wait_for_blender(timeout=60):
        return False

    try:
        _scene_manager.reload_scene()
        logger.info("%s: scene reloaded after crash", label)
        return True
    except Exception:
        logger.error("%s: failed to reload scene after crash", label, exc_info=True)
        return False


def _crash_user_message(label: str, original: Exception) -> str:
    """Build an actionable error string for the LLM when Blender crashed."""
    if _crash_count > _MAX_CRASHES:
        return (
            f"Blender has crashed {_crash_count} times in this conversation and the safety "
            f"circuit breaker has been tripped. Please ask the user to start a new "
            f"conversation. Last error from {label}: {original}"
        )
    return (
        f"Blender crashed during '{label}' (likely caused by the operation just "
        f"attempted — large/complex assets and certain glTF imports are common "
        f"triggers). The previous scene has been restored from the saved state. "
        f"Please apologize briefly to the user and try a different approach: "
        f"smaller resolution (e.g. '1k'), a simpler asset, or build the missing "
        f"geometry with primitives. Original error: {original}"
    )


# ──────────────────────────────────────────────
# Agent tools - Scene inspection
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def get_scene_info() -> str:
    """
    Get detailed information about the current Blender scene including
    all objects, their types, locations, and material counts.
    Always call this first to understand the current state of the scene.
    """
    logger.info("Tool called: get_scene_info")
    for attempt in (1, 2):
        try:
            blender = get_blender_connection()
            result = blender.send_command("get_scene_info")
            # Return a concise summary instead of raw JSON to save LLM tokens
            obj_count = result.get("object_count", 0)
            mat_count = result.get("materials_count", 0)
            lines = [f"Scene '{result.get('name', '?')}': {obj_count} objects, {mat_count} materials."]
            for obj in result.get("objects", []):
                loc = obj.get("location", [0, 0, 0])
                lines.append(f"  - {obj.get('name', '?')} ({obj.get('type', '?')}) at ({loc[0]}, {loc[1]}, {loc[2]})")
            logger.info("get_scene_info succeeded: %d objects, %d materials", obj_count, mat_count)
            return "\n".join(lines)
        except Exception as e:
            logger.error("get_scene_info failed (attempt %d/2)", attempt, exc_info=True)
            if attempt == 1 and _is_blender_crash_error(e):
                recovered = _recover_blender_scene("get_scene_info")
                if recovered:
                    continue  # retry once on the restored scene
            return f"Error getting scene info: {str(e)}"
    return "Error getting scene info: exhausted retries"


@tool(approval_mode="never_require")
def get_object_info(
    object_name: Annotated[str, "The exact name of the Blender object to inspect"],
) -> str:
    """
    Get detailed information about a specific object in the Blender scene
    including its location, rotation, scale, materials, and mesh data.
    """
    logger.info("Tool called: get_object_info(object_name=%r)", object_name)
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_object_info", {"name": object_name})
        # Return a concise summary instead of raw JSON to save LLM tokens
        lines = [f"Object '{result.get('name', object_name)}' ({result.get('type', '?')})"]
        loc = result.get("location", [0, 0, 0])
        if loc:
            lines.append(f"  Location: ({loc[0]}, {loc[1]}, {loc[2]})")
        rot = result.get("rotation", [0, 0, 0])
        if rot:
            lines.append(f"  Rotation: ({rot[0]}, {rot[1]}, {rot[2]})")
        sc = result.get("scale", [1, 1, 1])
        if sc:
            lines.append(f"  Scale: ({sc[0]}, {sc[1]}, {sc[2]})")
        mats = result.get("materials", [])
        if mats:
            mat_names = [m.get("name", "?") if isinstance(m, dict) else str(m) for m in mats]
            lines.append(f"  Materials: {', '.join(mat_names)}")
        if result.get("mesh"):
            mesh = result["mesh"]
            lines.append(f"  Mesh: {mesh.get('vertices', '?')} verts, {mesh.get('faces', '?')} faces")
        logger.info("get_object_info succeeded for %r", object_name)
        return "\n".join(lines)
    except Exception as e:
        logger.error("get_object_info failed for %r", object_name, exc_info=True)
        return f"Error getting object info: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Object creation & manipulation
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def create_object(
    object_type: Annotated[
        str,
        "Type of primitive to create: 'cube', 'sphere', 'cylinder', 'cone', 'torus', 'plane', 'monkey'",
    ],
    name: Annotated[str, "Name for the new object"] = "Object",
    location_x: Annotated[float, "X position"] = 0.0,
    location_y: Annotated[float, "Y position"] = 0.0,
    location_z: Annotated[float, "Z position"] = 0.0,
    scale_x: Annotated[float, "X scale"] = 1.0,
    scale_y: Annotated[float, "Y scale"] = 1.0,
    scale_z: Annotated[float, "Z scale"] = 1.0,
) -> str:
    """
    Create a 3D primitive object in the Blender scene.
    Supported types: cube, sphere, cylinder, cone, torus, plane, monkey.
    """
    ops_map = {
        "cube": "bpy.ops.mesh.primitive_cube_add",
        "sphere": "bpy.ops.mesh.primitive_uv_sphere_add",
        "cylinder": "bpy.ops.mesh.primitive_cylinder_add",
        "cone": "bpy.ops.mesh.primitive_cone_add",
        "torus": "bpy.ops.mesh.primitive_torus_add",
        "plane": "bpy.ops.mesh.primitive_plane_add",
        "monkey": "bpy.ops.mesh.primitive_monkey_add",
    }

    logger.info("Tool called: create_object(type=%r, name=%r, loc=(%s,%s,%s), scale=(%s,%s,%s))",
                object_type, name, location_x, location_y, location_z, scale_x, scale_y, scale_z)

    op = ops_map.get(object_type.lower())
    if not op:
        logger.warning("create_object: unknown type %r", object_type)
        return f"Error: Unknown object type '{object_type}'. Use one of: {', '.join(ops_map.keys())}"

    code = f"""
import bpy
{op}(location=({location_x}, {location_y}, {location_z}), scale=({scale_x}, {scale_y}, {scale_z}))
obj = bpy.context.active_object
obj.name = "{name}"
print(f"Created {{obj.name}} at ({{obj.location.x}}, {{obj.location.y}}, {{obj.location.z}})")
"""
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        logger.info("create_object succeeded: %s '%s'", object_type, name)
        return f"Created {object_type} named '{name}' at ({location_x}, {location_y}, {location_z})."
    except Exception as e:
        logger.error("create_object failed for type=%r name=%r", object_type, name, exc_info=True)
        return f"Error creating object: {str(e)}"


@tool(approval_mode="never_require")
def modify_object(
    object_name: Annotated[str, "Name of the object to modify"],
    location_x: Annotated[float, "New X position (or current if unchanged)"] = None,
    location_y: Annotated[float, "New Y position (or current if unchanged)"] = None,
    location_z: Annotated[float, "New Z position (or current if unchanged)"] = None,
    rotation_x: Annotated[float, "Rotation around X axis in degrees"] = None,
    rotation_y: Annotated[float, "Rotation around Y axis in degrees"] = None,
    rotation_z: Annotated[float, "Rotation around Z axis in degrees"] = None,
    scale_x: Annotated[float, "X scale factor"] = None,
    scale_y: Annotated[float, "Y scale factor"] = None,
    scale_z: Annotated[float, "Z scale factor"] = None,
) -> str:
    """
    Modify an existing object's transform (location, rotation, scale).
    Only the specified parameters will be changed.
    Rotation values are in degrees.
    """
    logger.info("Tool called: modify_object(object_name=%r)", object_name)

    # Guardrail: refuse no-op calls. If the model passes only object_name with no
    # transform parameters, the previous behaviour was to silently return success
    # without changing anything — confusing to users (e.g. "reduce size by 5x"
    # without the model first reading current scale via get_object_info).
    if all(
        v is None
        for v in (
            location_x, location_y, location_z,
            rotation_x, rotation_y, rotation_z,
            scale_x, scale_y, scale_z,
        )
    ):
        logger.warning("modify_object called with no transform parameters for %r", object_name)
        return (
            f"Error: modify_object('{object_name}') was called without any transform "
            "parameters — nothing was changed. modify_object takes ABSOLUTE values, "
            "not relative ones. For a relative change (e.g. 'reduce size by 5x'), "
            "first call get_object_info() to read the current scale/location/rotation, "
            "compute the new absolute value, and pass it as scale_x/scale_y/scale_z "
            "(or location_*/rotation_*) on the next call."
        )

    lines = ["import bpy", "import math"]
    lines.append(f'obj = bpy.data.objects.get("{object_name}")')
    lines.append("if not obj:")
    lines.append(f'    raise ValueError("Object not found: {object_name}")')

    if location_x is not None:
        lines.append(f"obj.location.x = {location_x}")
    if location_y is not None:
        lines.append(f"obj.location.y = {location_y}")
    if location_z is not None:
        lines.append(f"obj.location.z = {location_z}")
    if rotation_x is not None:
        lines.append(f"obj.rotation_euler.x = math.radians({rotation_x})")
    if rotation_y is not None:
        lines.append(f"obj.rotation_euler.y = math.radians({rotation_y})")
    if rotation_z is not None:
        lines.append(f"obj.rotation_euler.z = math.radians({rotation_z})")
    if scale_x is not None:
        lines.append(f"obj.scale.x = {scale_x}")
    if scale_y is not None:
        lines.append(f"obj.scale.y = {scale_y}")
    if scale_z is not None:
        lines.append(f"obj.scale.z = {scale_z}")

    lines.append(
        'print(f"Modified {obj.name}: loc=({obj.location.x:.2f}, {obj.location.y:.2f}, {obj.location.z:.2f})")'
    )

    code = "\n".join(lines)

    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        logger.info("modify_object succeeded for %r", object_name)
        return f"Modified '{object_name}'."
    except Exception as e:
        logger.error("modify_object failed for %r", object_name, exc_info=True)
        return f"Error modifying object: {str(e)}"


@tool(approval_mode="never_require")
def delete_object(
    object_name: Annotated[str, "Name of the object to delete"],
) -> str:
    """Delete an object from the Blender scene by name."""
    logger.info("Tool called: delete_object(object_name=%r)", object_name)
    code = f"""
import bpy
obj = bpy.data.objects.get("{object_name}")
if not obj:
    raise ValueError("Object not found: {object_name}")
bpy.data.objects.remove(obj, do_unlink=True)
print("Deleted {object_name}")
"""
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        logger.info("delete_object succeeded for %r", object_name)
        return f"Deleted '{object_name}'."
    except Exception as e:
        logger.error("delete_object failed for %r", object_name, exc_info=True)
        return f"Error deleting object: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Materials & appearance
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def apply_material(
    object_name: Annotated[str, "Name of the object to apply material to"],
    color_hex: Annotated[
        str,
        "Hex color code (e.g. '#FF0000' for red, '#00FF00' for green)",
    ],
    material_name: Annotated[str, "Name for the material"] = None,
    metallic: Annotated[float, "Metallic value 0.0-1.0"] = 0.0,
    roughness: Annotated[float, "Roughness value 0.0-1.0"] = 0.5,
) -> str:
    """
    Create and apply a material with the specified color to an object.
    Color should be a hex code like '#FF0000' for red.
    """
    logger.info("Tool called: apply_material(object=%r, color=%r, metallic=%s, roughness=%s)",
                object_name, color_hex, metallic, roughness)
    mat_name = material_name or f"Material_{object_name}"
    code = f"""
import bpy

hex_color = "{color_hex}".lstrip('#')
r = int(hex_color[0:2], 16) / 255.0
g = int(hex_color[2:4], 16) / 255.0
b = int(hex_color[4:6], 16) / 255.0

obj = bpy.data.objects.get("{object_name}")
if not obj:
    raise ValueError("Object not found: {object_name}")

mat = bpy.data.materials.new(name="{mat_name}")
mat.use_nodes = True
principled = mat.node_tree.nodes.get("Principled BSDF")
if principled:
    principled.inputs["Base Color"].default_value = (r, g, b, 1.0)
    principled.inputs["Metallic"].default_value = {metallic}
    principled.inputs["Roughness"].default_value = {roughness}

# Clear existing materials and apply new one
while len(obj.data.materials) > 0:
    obj.data.materials.pop(index=0)
obj.data.materials.append(mat)

print(f"Applied material '{{mat.name}}' with color ({color_hex}) to {{obj.name}}")
"""
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        logger.info("apply_material succeeded for %r with color %s", object_name, color_hex)
        return f"Applied {color_hex} material to '{object_name}'."
    except Exception as e:
        logger.error("apply_material failed for %r", object_name, exc_info=True)
        return f"Error applying material: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Code execution
# ──────────────────────────────────────────────


# Error categories and hints for enriched error context
_ERROR_HINTS = [
    (
        re.compile(r"Object '(.+?)' not in collection '(.+?)'"),
        lambda m: (
            f"The object '{m.group(1)}' is not in '{m.group(2)}'. "
            f"This usually happens because bpy.ops.*_add() places new objects "
            f"in the ACTIVE collection, not necessarily Scene Collection. "
            f"Use `safe_move_to_collection(obj, target_collection)` (available "
            f"in the execution namespace) to safely move objects between collections, "
            f"or use `ensure_active_collection(collection)` before creating objects "
            f"so they are placed directly in the desired collection."
        ),
    ),
    (
        re.compile(r"Object '(.+?)' not found", re.IGNORECASE),
        lambda m: (
            f"The object '{m.group(1)}' does not exist in the scene. "
            f"Check the exact object names using get_scene_info()."
        ),
    ),
    (
        re.compile(r"read-only", re.IGNORECASE),
        lambda _: (
            "You tried to set a read-only attribute. "
            "collection.name is read-only — use bpy.data.collections.new('Name') instead."
        ),
    ),
    (
        re.compile(r"has no attribute '(\w+)'"),
        lambda m: (
            f"Attribute '{m.group(1)}' does not exist. This may be a Blender 3.x API "
            f"that was renamed or removed in Blender 4.x. Check the Blender 4.x API docs."
        ),
    ),
]


def _enrich_error_context(error_msg: str, code: str) -> str:
    """Build an enriched error message with scene state and categorized hints."""
    parts = [f"Error executing code: {error_msg}"]

    # Add categorized hint
    for pattern, hint_fn in _ERROR_HINTS:
        m = pattern.search(error_msg)
        if m:
            parts.append(f"\nHint: {hint_fn(m)}")
            break

    # Fetch current scene state to give the LLM context for self-correction
    try:
        blender = get_blender_connection()
        scene_result = blender.send_command("get_scene_info")
        objects = scene_result.get("objects", [])
        if objects:
            obj_names = [f"  - {o.get('name', '?')} ({o.get('type', '?')})" for o in objects[:30]]
            parts.append(f"\nCurrent scene objects ({len(objects)} total):")
            parts.extend(obj_names)
            if len(objects) > 30:
                parts.append(f"  ... and {len(objects) - 30} more")

        # Query collection names via a lightweight code execution
        try:
            col_result = blender.send_command("execute_code", {
                "code": (
                    "import bpy\n"
                    "for c in bpy.data.collections:\n"
                    "    print(f'{c.name} ({len(c.objects)} objects)')\n"
                )
            })
            col_output = col_result.get("result", "").strip()
            if col_output:
                parts.append(f"\nCollections:")
                for line in col_output.splitlines()[:20]:
                    parts.append(f"  - {line}")
        except Exception:
            pass  # Collection info is supplementary — skip on failure
    except Exception:
        parts.append("\n(Could not fetch scene state — Blender may be recovering.)")

    parts.append(
        "\nPlease fix the code and try again. The execution namespace includes "
        "safe helpers: safe_move_to_collection(obj, collection), "
        "safe_link_to_collection(obj, collection), "
        "ensure_active_collection(collection)."
    )

    return "\n".join(parts)


@tool(approval_mode="never_require")
def execute_blender_code(
    code: Annotated[
        str,
        "Python code to execute in Blender. Has access to 'bpy' module. Break complex operations into smaller steps.",
    ],
) -> str:
    """
    Execute arbitrary Python code inside Blender.
    The code has access to the 'bpy' module for full Blender API access.
    Use this for complex operations not covered by other tools.
    Break large operations into smaller steps for reliability.
    """
    logger.info("Tool called: execute_blender_code (code length=%d)", len(code))
    logger.debug("execute_blender_code code:\n%s", code)

    # ── Blender 4.x compatibility patches (defense-in-depth) ──
    _compat_replacements = [
        ("ShaderNodeTexMusgrave", "ShaderNodeTexNoise"),
        ("ShaderNodeMixRGB", "ShaderNodeMix"),
        ("inputs['Specular']", "inputs['Specular IOR Level']"),
        ('inputs["Specular"]', 'inputs["Specular IOR Level"]'),
        ("inputs['Subsurface']", "inputs['Subsurface Weight']"),
        ('inputs["Subsurface"]', 'inputs["Subsurface Weight"]'),
        ("inputs['Transmission']", "inputs['Transmission Weight']"),
        ('inputs["Transmission"]', 'inputs["Transmission Weight"]'),
        ("inputs['Emission']", "inputs['Emission Color']"),
        ('inputs["Emission"]', 'inputs["Emission Color"]'),
        ("inputs['Clearcoat']", "inputs['Coat']"),
        ('inputs["Clearcoat"]', 'inputs["Coat"]'),
        ("inputs['Clearcoat Roughness']", "inputs['Coat Roughness']"),
        ('inputs["Clearcoat Roughness"]', 'inputs["Coat Roughness"]'),
        ("inputs['Sheen']", "inputs['Sheen Weight']"),
        ('inputs["Sheen"]', 'inputs["Sheen Weight"]'),
    ]
    for old, new in _compat_replacements:
        if old in code:
            logger.warning("Blender 4.x compat: replacing deprecated '%s' → '%s'", old, new)
            code = code.replace(old, new)

    # ── Auto-patch unsafe collection patterns ──
    # Pattern: scene.collection.objects.unlink(VAR)
    # Wraps in try/except so it won't crash if the object isn't in Scene Collection.
    # The safe_move_to_collection helper is available in the exec namespace as a
    # better alternative, but this catches LLM code that uses the raw pattern.
    _unlink_pattern = re.compile(
        r'^(\s*)'                                         # leading whitespace
        r'(?:bpy\.context\.scene\.collection|scene\.collection)'
        r'\.objects\.unlink\((\w+)\)',                    # .objects.unlink(var)
        re.MULTILINE,
    )
    if _unlink_pattern.search(code):
        logger.warning(
            "Auto-patch: wrapping unsafe scene.collection.objects.unlink() "
            "in safe fallback (object may not be in Scene Collection)"
        )

        def _wrap_unlink(m):
            indent = m.group(1)
            var = m.group(2)
            # Replace with safe_move_to_collection-aware fallback:
            # unlink from whichever collection(s) the object is actually in
            return (
                f"{indent}for _col in list({var}.users_collection):\n"
                f"{indent}    _col.objects.unlink({var})"
            )

        code = _unlink_pattern.sub(_wrap_unlink, code)

    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        raw = result.get('result', '')
        # Truncate verbose Blender stdout to save LLM tokens
        if len(raw) > 500:
            raw = raw[:500] + "... [truncated]"
        logger.info("execute_blender_code succeeded")
        return f"Code executed successfully. {raw}".strip()
    except Exception as e:
        if _is_blender_crash_error(e):
            logger.error("execute_blender_code: Blender crashed", exc_info=True)
            recovered = _recover_blender_scene("execute_blender_code")
            if recovered:
                return _crash_user_message("execute_blender_code", e)
            return (
                f"Blender crashed while executing code and could not recover. "
                f"Please ask the user to retry. Original error: {e}"
            )
        logger.warning("execute_blender_code failed (will enrich error for LLM): %s", e)
        return _enrich_error_context(str(e), code)


# ──────────────────────────────────────────────
# Agent tools - Viewport screenshot
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def get_viewport_screenshot(
    max_size: Annotated[
        int,
        "Maximum size in pixels for the largest dimension (default: 800)",
    ] = 800,
) -> str:
    """
    Capture a screenshot of the current Blender 3D viewport.
    Returns the screenshot as a base64-encoded PNG image string.
    Use this to show the user what the scene looks like.
    """
    logger.info("Tool called: get_viewport_screenshot(max_size=%d)", max_size)
    try:
        blender = get_blender_connection()
        # Unique timestamped filename — kept in $HOME/tmp AND uploaded to blob
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        unique_name = f"screenshot_{timestamp}_{uuid.uuid4().hex[:8]}.png"
        local_path = os.path.join(_PERSISTENT_TMP, unique_name)

        result = blender.send_command(
            "get_viewport_screenshot",
            {"max_size": max_size, "filepath": local_path, "format": "png"},
        )

        if "error" in result:
            return f"Screenshot error: {result['error']}"

        if not os.path.exists(local_path):
            return "Error: Screenshot file was not created"

        with open(local_path, "rb") as f:
            image_bytes = f.read()

        # Upload to Azure Blob Storage and return URL (local copy stays in $HOME/tmp)
        blob_url = upload_image_to_blob(image_bytes, unique_name)
        width = result.get('width', '?')
        height = result.get('height', '?')
        logger.info("get_viewport_screenshot succeeded: %sx%s, saved to %s and uploaded to blob",
                    width, height, local_path)
        return (
            f"Screenshot captured ({width}x{height} pixels). "
            f"The image has ALREADY been displayed to the user automatically. "
            f"Do NOT include, repeat, or reproduce the image markdown in your reply — "
            f"just confirm briefly in one short sentence.\n\n![screenshot]({blob_url})"
        )
    except Exception as e:
        logger.error("get_viewport_screenshot failed", exc_info=True)
        return f"Error capturing screenshot: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - 3D model library (Microsoft)
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def list_available_models(
    query: Annotated[
        str,
        "What kind of 3D model to look for in the library, e.g. 'chair', 'dog', "
        "'spaceship'. A short noun phrase works best.",
    ],
) -> str:
    """Search Microsoft's public 3D-model library for downloadable GLB models.

    Returns a JSON array (as a string) of up to a few models, each shaped like
    {"name": str, "imageUrl": str, "modelUrl": str}:
      * imageUrl is a thumbnail preview the web client shows in the chat gallery.
      * modelUrl is the GLB download link to pass to `download_model` when the
        user picks one.
    Returns "[]" when nothing matches, or a string starting with "ERROR:" on failure.
    """
    logger.info("Tool called: list_available_models(query=%r)", query)
    payload = {
        "type": "Search",
        "pageSize": MODEL_SEARCH_PAGE_SIZE,
        "query": query,
        "parameters": {"firstpartycontent": False, "app": "office"},
        "descriptor": {"$type": "FirstPartyContentSearchDescriptor"},
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                MODEL_SEARCH_URL,
                json=payload,
                headers={"Content-Type": "application/json", **_HTTP_HEADERS},
            )
            response.raise_for_status()
            content = response.json()
    except Exception as e:
        logger.error("list_available_models transport error", exc_info=True)
        return f"ERROR: could not reach the 3D-model library ({e})."

    result = content.get("Result") or {}
    part_groups = result.get("PartGroups") or []
    models: list[dict[str, str]] = []
    for group in part_groups:
        image_parts = group.get("ImageParts") or []
        image_url = image_parts[0].get("SourceUrl") if image_parts else None

        title = None
        model_url = None
        for text_part in group.get("TextParts") or []:
            category = text_part.get("TextCategory")
            if category == "Title":
                title = text_part.get("Text")
            elif category == "OasisGlbLink":
                model_url = text_part.get("Text")

        # Only surface entries that have everything the client needs to show + load.
        if title and image_url and model_url:
            models.append({"name": title, "imageUrl": image_url, "modelUrl": model_url})

    logger.info("list_available_models: %d model(s) found", len(models))
    return json.dumps(models)


@tool(approval_mode="never_require")
def download_model(
    model_url: Annotated[
        str,
        "The GLB download link (modelUrl) of the model to import, taken from a "
        "previous list_available_models result.",
    ],
    name: Annotated[
        str,
        "A short, descriptive name for the imported model. Becomes the Blender "
        "object name so later turns can reference it (e.g. 'red_chair').",
    ],
) -> str:
    """Download a chosen GLB model from the library and import it into the scene.

    After it succeeds, take ONE viewport screenshot so the user sees the result.
    Returns a confirmation with the imported object names, or a message starting
    with "Error"/"Blender crashed" on failure.
    """
    logger.info("Tool called: download_model(name=%r, url=%r)", name, model_url)
    try:
        blender = get_blender_connection()
        result = blender.send_command(
            "import_model_from_url",
            {"model_url": model_url, "name": name},
        )

        if "error" in result:
            return f"Error: {result['error']}"

        if result.get("success"):
            imported = ", ".join(result.get("imported_objects", [])) or name
            logger.info("download_model succeeded: %s", imported)
            return (
                f"Imported the model into the scene as: {imported}. "
                f"Now take a single viewport screenshot so the user can see it."
            )
        logger.warning("download_model failed: %s", result.get("message", "Unknown error"))
        return f"Failed to import model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error("download_model failed for url=%r", model_url, exc_info=True)
        if _is_blender_crash_error(e):
            recovered = _recover_blender_scene("download_model")
            if recovered:
                return _crash_user_message(f"download_model(name={name!r})", e)
            return (
                f"Blender crashed while importing the model and could not recover. "
                f"Please ask the user to retry; the saved scene was preserved. "
                f"Original error: {e}"
            )
        return f"Error importing model: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Poly Haven textures
# ──────────────────────────────────────────────


def _score_texture(asset_id: str, meta: dict, tokens: list[str]) -> int:
    """Rank a Poly Haven texture asset against the search tokens.

    Higher weight for hits in the human name / id, then categories, then tags.
    """
    name_hay = f"{meta.get('name', '')} {asset_id}".lower()
    tags = [str(t).lower() for t in (meta.get("tags") or [])]
    cats = [str(c).lower() for c in (meta.get("categories") or [])]
    score = 0
    for tok in tokens:
        if tok in name_hay:
            score += 3
        if tok in cats:
            score += 2
        elif any(tok in c for c in cats):
            score += 1
        if tok in tags:
            score += 2
        elif any(tok in t for t in tags):
            score += 1
    return score


@tool(approval_mode="never_require")
def list_available_textures(
    query: Annotated[
        str,
        "What kind of surface texture to look for, e.g. 'red brick', 'mossy rock', "
        "'wood planks', 'concrete', 'sand'. A short descriptive phrase works best.",
    ],
) -> str:
    """Search the Poly Haven library for free PBR surface textures.

    Returns a JSON array (as a string) of up to a few textures, each shaped like
    {"name": str, "imageUrl": str, "assetId": str}:
      * imageUrl is a thumbnail preview the web client shows in the chat gallery.
      * assetId is the Poly Haven id to pass to `apply_texture` when the user
        picks one and names an object to apply it to.
    Returns "[]" when nothing matches, or a string starting with "ERROR:" on failure.
    """
    logger.info("Tool called: list_available_textures(query=%r)", query)
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(
                f"{POLYHAVEN_API}/assets",
                params={"t": "textures"},
                headers=_HTTP_HEADERS,
            )
            response.raise_for_status()
            assets = response.json()
    except Exception as e:
        logger.error("list_available_textures transport error", exc_info=True)
        return f"ERROR: could not reach the Poly Haven library ({e})."

    if not isinstance(assets, dict):
        return "ERROR: unexpected response from the Poly Haven library."

    tokens = [t for t in query.lower().split() if t]
    scored: list[tuple[int, str, dict]] = []
    for asset_id, meta in assets.items():
        if not isinstance(meta, dict):
            continue
        score = _score_texture(asset_id, meta, tokens) if tokens else 0
        if score > 0:
            scored.append((score, asset_id, meta))

    scored.sort(key=lambda item: item[0], reverse=True)
    textures: list[dict[str, str]] = []
    for _score, asset_id, meta in scored[:TEXTURE_SEARCH_PAGE_SIZE]:
        thumb = meta.get("thumbnail_url") or (
            f"https://cdn.polyhaven.com/asset_img/thumbs/{asset_id}.png?width=256&height=256"
        )
        textures.append(
            {
                "name": meta.get("name") or asset_id,
                "imageUrl": thumb,
                "assetId": asset_id,
            }
        )

    logger.info("list_available_textures: %d texture(s) found", len(textures))
    return json.dumps(textures)


@tool(approval_mode="never_require")
def apply_texture(
    asset_id: Annotated[
        str,
        "The Poly Haven texture id (assetId) to apply, taken from a previous "
        "list_available_textures result, e.g. 'brick_wall_006'.",
    ],
    object_name: Annotated[
        str,
        "The name of the object in the scene to apply the texture to (e.g. "
        "'ground', 'wall1', 'red_chair').",
    ],
    resolution: Annotated[
        str,
        "Texture resolution to download: '1k', '2k', or '4k'. Use '2k' unless the "
        "user asks for sharper/heavier ('4k') or lighter ('1k') textures.",
    ] = TEXTURE_DEFAULT_RESOLUTION,
) -> str:
    """Download a Poly Haven PBR texture and apply it as a material to an object.

    Downloads the texture's maps into Blender, builds a Principled BSDF material,
    and assigns it to `object_name`. After it succeeds, take ONE viewport
    screenshot so the user sees the result.
    """
    resolution = (resolution or TEXTURE_DEFAULT_RESOLUTION).lower()
    logger.info(
        "Tool called: apply_texture(asset=%r, object=%r, res=%r)",
        asset_id, object_name, resolution,
    )
    try:
        blender = get_blender_connection()
        # 1) Download the texture maps into Blender (creates a material + images
        #    named "<asset_id>_<map>").
        dl = blender.send_command(
            "download_polyhaven_asset",
            {
                "asset_id": asset_id,
                "asset_type": "textures",
                "resolution": resolution,
                "file_format": None,
            },
        )
        if "error" in dl:
            return f"Error downloading texture '{asset_id}': {dl['error']}"

        # 2) Build a material from those maps and assign it to the object.
        result = blender.send_command(
            "set_texture",
            {"object_name": object_name, "texture_id": asset_id},
        )
        if "error" in result:
            return f"Error: {result['error']}"

        if result.get("success"):
            logger.info("apply_texture succeeded: %r -> %r", asset_id, object_name)
            return (
                f"Applied texture '{asset_id}' to '{object_name}'. "
                f"Now take a single viewport screenshot so the user can see it."
            )
        logger.warning("apply_texture failed: %s", result.get("message", "Unknown error"))
        return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(
            "apply_texture failed for asset=%r object=%r",
            asset_id, object_name, exc_info=True,
        )
        if _is_blender_crash_error(e):
            recovered = _recover_blender_scene("apply_texture")
            if recovered:
                return _crash_user_message(
                    f"apply_texture(asset={asset_id!r}, object={object_name!r})", e
                )
            return (
                f"Blender crashed while applying the texture and could not recover. "
                f"Please ask the user to retry; the saved scene was preserved. "
                f"Original error: {e}"
            )
        return f"Error applying texture: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Scene setup helpers
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def setup_scene(
    clear_default: Annotated[
        bool,
        "Whether to clear the default cube, camera, and light",
    ] = True,
    add_camera: Annotated[bool, "Whether to add a camera"] = True,
    camera_location: Annotated[
        str,
        "Camera location as 'x,y,z' (e.g. '7,-5,5')",
    ] = "7,-5,5",
    add_light: Annotated[bool, "Whether to add a sun light"] = True,
    add_ground_plane: Annotated[bool, "Whether to add a ground plane"] = False,
) -> str:
    """
    Set up the Blender scene with camera, lighting, and optionally a ground plane.
    This is a good first step when creating a new scene.
    """
    logger.info("Tool called: setup_scene(clear=%s, camera=%s, light=%s, ground=%s)",
                clear_default, add_camera, add_light, add_ground_plane)

    # Guardrail: refuse to wipe a populated scene on a continuation turn.
    # The system prompt tells the model to call setup_scene as the first action
    # on a new scene, but on follow-up turns the model sometimes re-issues
    # setup_scene(clear_default=True) which previously deleted the user's work.
    # If clear_default=True and there are user objects (anything that isn't a
    # camera or light), refuse and ask the LLM to skip clearing.
    if clear_default:
        try:
            blender = get_blender_connection()
            probe = blender.send_command("execute_code", {
                "code": (
                    "import bpy\n"
                    "user_objs = [o.name for o in bpy.data.objects "
                    "if o.type not in {'CAMERA', 'LIGHT'}]\n"
                    "print('USER_OBJECTS=' + ','.join(user_objs))\n"
                ),
            })
            probe_out = (probe or {}).get("result", "") if isinstance(probe, dict) else str(probe)
            marker = "USER_OBJECTS="
            existing: list[str] = []
            if marker in probe_out:
                line = probe_out.split(marker, 1)[1].splitlines()[0].strip()
                existing = [n for n in line.split(",") if n]
            if existing:
                logger.warning(
                    "setup_scene(clear=True) blocked: scene has %d user object(s): %s",
                    len(existing), existing,
                )
                preview = ", ".join(existing[:8]) + ("…" if len(existing) > 8 else "")
                return (
                    f"Refused: setup_scene(clear_default=True) would delete {len(existing)} "
                    f"existing object(s) ({preview}) from this conversation's scene. "
                    "Only call setup_scene at the START of a brand-new scene. On a "
                    "continuation turn, call get_scene_info() to inspect what already "
                    "exists, then add/modify objects directly without clearing. If you "
                    "truly want to start over, call delete_object on each object first "
                    "or call setup_scene(clear_default=False)."
                )
        except Exception:
            # Probe failures shouldn't block the operation — fall through and let
            # the actual setup_scene code path surface a clearer error.
            logger.debug("setup_scene clear-guard probe failed; continuing", exc_info=True)

    cam_parts = [float(x.strip()) for x in camera_location.split(",")]
    cam_x, cam_y, cam_z = cam_parts[0], cam_parts[1], cam_parts[2]

    code = "import bpy\nimport math\nimport mathutils\n\n"

    if clear_default:
        code += """# Clear default objects
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)
print("Cleared all default objects")

"""

    if add_camera:
        code += f"""# Add camera
bpy.ops.object.camera_add(location=({cam_x}, {cam_y}, {cam_z}))
camera = bpy.context.active_object
camera.name = "Camera"

# Point camera at origin
direction = mathutils.Vector((0, 0, 0)) - camera.location
rot_quat = direction.to_track_quat('-Z', 'Y')
camera.rotation_euler = rot_quat.to_euler()
bpy.context.scene.camera = camera
print(f"Added camera at ({{camera.location.x:.1f}}, {{camera.location.y:.1f}}, {{camera.location.z:.1f}})")

"""

    if add_light:
        code += """# Add sun light
bpy.ops.object.light_add(type='SUN', location=(5, -3, 8))
light = bpy.context.active_object
light.name = "Sun"
light.data.energy = 3.0
print("Added sun light")

"""

    if add_ground_plane:
        code += """# Add ground plane
bpy.ops.mesh.primitive_plane_add(size=20, location=(0, 0, 0))
ground = bpy.context.active_object
ground.name = "Ground"
mat = bpy.data.materials.new(name="GroundMaterial")
mat.use_nodes = True
principled = mat.node_tree.nodes.get("Principled BSDF")
if principled:
    principled.inputs["Base Color"].default_value = (0.3, 0.3, 0.3, 1.0)
    principled.inputs["Roughness"].default_value = 0.8
ground.data.materials.append(mat)
print("Added ground plane")

"""

    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        logger.info("setup_scene succeeded")
        return "Scene setup complete."
    except Exception as e:
        logger.error("setup_scene failed", exc_info=True)
        return f"Error setting up scene: {str(e)}"


def _do_render(
    label: str,
    resolution_x: int,
    resolution_y: int,
    samples: int,
    engine: str,
) -> str:
    """Shared render helper used by render_preview and render_final.

    Generates a unique timestamped output path in $HOME/tmp, keeps the local
    file after upload. Includes a recovery mechanism: if the Blender connection
    drops (e.g. Blender crashes mid-render), waits for the supervisor to restart
    it, reloads the conversation's scene, and retries once.
    """
    # Generate unique timestamped output path — kept in $HOME/tmp AND uploaded to blob
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    unique_name = f"{label}_{timestamp}_{uuid.uuid4().hex[:8]}.png"
    render_path = os.path.join(_PERSISTENT_TMP, unique_name)

    logger.info("%s: rendering %dx%d, samples=%d, engine=%r, output=%s",
                label, resolution_x, resolution_y, samples, engine, render_path)
    code = f"""
import bpy

bpy.context.scene.render.engine = '{engine}'
bpy.context.scene.render.resolution_x = {resolution_x}
bpy.context.scene.render.resolution_y = {resolution_y}
bpy.context.scene.render.filepath = '{render_path}'
bpy.context.scene.render.image_settings.file_format = 'PNG'

if '{engine}' == 'CYCLES':
    bpy.context.scene.cycles.samples = {samples}
    bpy.context.scene.cycles.device = 'CPU'

bpy.ops.render.render(write_still=True)
print("Render complete: {render_path}")
"""
    max_attempts = 2  # first try + one recovery retry
    for attempt in range(1, max_attempts + 1):
        try:
            blender = get_blender_connection()
            result = blender.send_command("execute_code", {"code": code})

            if os.path.exists(render_path):
                with open(render_path, "rb") as f:
                    image_bytes = f.read()
                # Upload to blob (local copy stays in $HOME/tmp)
                blob_url = upload_image_to_blob(image_bytes, unique_name)
                logger.info("%s succeeded: %dx%d, saved to %s and uploaded to blob",
                            label, resolution_x, resolution_y, render_path)
                return (
                    f"Render complete ({resolution_x}x{resolution_y}). "
                    f"The image has ALREADY been displayed to the user automatically. "
                    f"Do NOT include, repeat, or reproduce the image markdown in your reply — "
                    f"just confirm briefly in one short sentence.\n\n![{label}]({blob_url})"
                )
            else:
                logger.warning("%s: output file not found at %r", label, render_path)
                return f"Render command executed but output file not found. {result.get('result', '')}"

        except Exception as e:
            logger.error("%s failed (attempt %d/%d)", label, attempt, max_attempts, exc_info=True)

            # Only treat connection-level failures as Blender crashes. Regular
            # runtime errors raised by bpy (e.g. "Cannot render, no camera",
            # missing material, bad filepath) must be returned to the model
            # verbatim so it can self-correct — the previous behaviour reset
            # the scene on every render error, wiping all in-progress work.
            if not _is_blender_crash_error(e):
                # Enrich the error so the model knows what to fix.
                hint = _render_error_hint(e)
                return f"Error rendering: {str(e)}{hint}"

            if attempt >= max_attempts:
                return f"Error rendering: {str(e)}"

            # ── Recovery: wait for Blender to come back, reload scene, retry ──
            logger.info("%s: attempting recovery — waiting for Blender to restart…", label)
            recovered = _wait_for_blender(timeout=60)
            if not recovered:
                return f"Error rendering: Blender did not recover after crash. {e}"

            # Reload the scene into the fresh Blender instance
            if _scene_manager:
                try:
                    logger.info("%s: reloading scene after Blender restart", label)
                    _scene_manager.activate_scene()
                except Exception as restore_err:
                    logger.error("%s: failed to restore scene after recovery: %s", label, restore_err)
                    return f"Error rendering: Blender recovered but scene restore failed. {restore_err}"

            logger.info("%s: recovery complete, retrying render…", label)

    return f"Error rendering: exhausted all {max_attempts} attempts"


def _render_error_hint(err: Exception) -> str:
    """Build a one-line diagnostic hint appended to render error messages.

    Probes Blender for the current camera + object count so the model can
    self-correct (e.g. add a camera or assign ``scene.camera``).
    """
    msg = str(err).lower()
    try:
        blender = get_blender_connection()
        probe = blender.send_command("execute_code", {
            "code": (
                "import bpy\n"
                "s = bpy.context.scene\n"
                "cam = s.camera.name if s.camera else 'None'\n"
                "print(f'scene={s.name!r} camera={cam} objects={len(bpy.data.objects)}')\n"
            ),
        })
        state = (probe.get("result") or "").strip().splitlines()
        state_line = state[-1] if state else "(unknown)"
    except Exception:
        state_line = "(scene probe failed)"

    if "no camera" in msg:
        return (
            f"\nHint: bpy.ops.render.render() requires bpy.context.scene.camera to be set. "
            f"Current state: {state_line}. Add a camera with bpy.ops.object.camera_add(...) "
            f"and assign it via bpy.context.scene.camera = <camera_object>, then retry the render. "
            f"Do NOT call setup_scene again — that would clear the scene."
        )
    return f"\nState at failure: {state_line}"


def _wait_for_blender(timeout: int = 60) -> bool:
    """Poll until Blender's socket server is reachable or timeout is exceeded."""
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            get_blender_connection()
            logger.info("Blender connection re-established after restart")
            return True
        except Exception:
            _time.sleep(2)
    logger.error("Blender did not become reachable within %ds", timeout)
    return False


@tool(approval_mode="never_require")
def render_preview(
    resolution_x: Annotated[int, "Preview width in pixels"] = 960,
    resolution_y: Annotated[int, "Preview height in pixels"] = 540,
    samples: Annotated[int, "Number of preview samples (keep low for speed)"] = 16,
    engine: Annotated[str, "Render engine: 'CYCLES' or 'BLENDER_EEVEE_NEXT'"] = "CYCLES",
) -> str:
    """
    Fast low-resolution preview render. Use this FIRST so the user can see a
    quick result before committing to a full-quality render.
    """
    return _do_render("preview", resolution_x, resolution_y, samples, engine)


@tool(approval_mode="never_require")
def render_final(
    resolution_x: Annotated[int, "Render width in pixels"] = 640,
    resolution_y: Annotated[int, "Render height in pixels"] = 480,
    samples: Annotated[int, "Number of render samples (higher = better quality)"] = 32,
    engine: Annotated[str, "Render engine: 'CYCLES' or 'BLENDER_EEVEE_NEXT'"] = "CYCLES",
) -> str:
    """
    High-fidelity render. Default resolution is 640x480 at 32 samples.
    For higher resolutions requested by the user, first render a quick
    640x480 preview, return it, and ask the user to confirm before
    proceeding with the full resolution at 256 samples.
    """
    return _do_render("render", resolution_x, resolution_y, samples, engine)


# ──────────────────────────────────────────────
# Agent tools - Scene download
# ──────────────────────────────────────────────


@tool(approval_mode="never_require")
def save_scene_for_download() -> str:
    """
    Save the current Blender scene as a .blend file and upload it to cloud storage.
    Returns a download link so the user can open the scene on their own machine.
    The link expires after 1 hour.
    """
    logger.info("Tool called: save_scene_for_download")
    try:
        blender = get_blender_connection()
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"blender_scene_{uuid.uuid4().hex[:8]}.blend")
        safe_path = temp_path.replace("\\", "/")

        code = f"""import bpy
bpy.ops.wm.save_as_mainfile(filepath="{safe_path}", check_existing=False)
print("Saved scene to {safe_path}")
"""
        result = blender.send_command("execute_code", {"code": code})

        if not os.path.exists(temp_path):
            return "Error: Blender save command ran but the .blend file was not created."

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        blob_name = f"scenes/scene_{timestamp}_{uuid.uuid4().hex[:8]}.blend"
        blob_url = upload_blend_to_blob(temp_path, blob_name)

        file_size = os.path.getsize(temp_path)
        os.remove(temp_path)

        logger.info("save_scene_for_download succeeded: %d bytes, uploaded to blob", file_size)
        return (
            f"Scene saved ({file_size / 1024:.0f} KB). "
            f"The download link has ALREADY been displayed to the user automatically. "
            f"Do NOT include, repeat, or reproduce the download link markdown in your reply — "
            f"just confirm briefly in one short sentence.\n\n"
            f"[Download your Blender scene (.blend)]({blob_url})"
        )
    except Exception as e:
        logger.error("save_scene_for_download failed", exc_info=True)
        return f"Error saving scene for download: {str(e)}"


@tool(approval_mode="never_require")
def export_scene_as_glb_for_download() -> str:
    """
    Export the current Blender scene as a binary glTF (.glb) file using Blender's
    built-in glTF exporter and upload it to cloud storage.
    Returns a download link the user can use to open the scene in any glTF viewer
    (Babylon.js Sandbox, three.js editor, Windows 3D Viewer, etc.).
    The link expires after 1 hour.
    """
    logger.info("Tool called: export_scene_as_glb_for_download")
    try:
        blender = get_blender_connection()
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(temp_dir, f"blender_scene_{uuid.uuid4().hex[:8]}.glb")
        safe_path = temp_path.replace("\\", "/")

        code = f"""import bpy
bpy.ops.export_scene.gltf(
    filepath="{safe_path}",
    export_format='GLB',
    use_selection=False,
)
print("Exported scene to {safe_path}")
"""
        result = blender.send_command("execute_code", {"code": code})

        if not os.path.exists(temp_path):
            return "Error: Blender export command ran but the .glb file was not created."

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        blob_name = f"scenes/scene_{timestamp}_{uuid.uuid4().hex[:8]}.glb"
        blob_url = upload_glb_to_blob(temp_path, blob_name)

        file_size = os.path.getsize(temp_path)
        os.remove(temp_path)

        logger.info("export_scene_as_glb_for_download succeeded: %d bytes, uploaded to blob", file_size)
        return (
            f"Scene exported as GLB ({file_size / 1024:.0f} KB). "
            f"The download link has ALREADY been displayed to the user automatically. "
            f"Do NOT include, repeat, or reproduce the download link markdown in your reply — "
            f"just confirm briefly in one short sentence.\n\n"
            f"[Download your Blender scene (.glb)]({blob_url})"
        )
    except Exception as e:
        logger.error("export_scene_as_glb_for_download failed", exc_info=True)
        return f"Error exporting scene as GLB: {str(e)}"


# ──────────────────────────────────────────────
# Middleware - stream status updates to the client
# ──────────────────────────────────────────────

_TOOL_STATUS_MESSAGES: dict[str, str] = {
    "get_scene_info": "Inspecting the scene…",
    "get_object_info": "Getting object details…",
    "create_object": "Creating object…",
    "modify_object": "Modifying object…",
    "delete_object": "Deleting object…",
    "apply_material": "Applying material…",
    "execute_blender_code": "Executing Blender code…",
    "get_viewport_screenshot": "Capturing viewport screenshot…",
    "list_available_models": "Searching the 3D model library…",
    "download_model": "Importing the 3D model…",
    "list_available_textures": "Searching Poly Haven textures…",
    "apply_texture": "Applying the texture…",
    "setup_scene": "Setting up the scene…",
    "render_preview": "Rendering a quick preview…",
    "render_final": "Rendering the final image (this may take a moment)…",
    "save_scene_for_download": "Saving scene for download…",
    "export_scene_as_glb_for_download": "Exporting scene as GLB…",
}


class ToolStatusMiddleware(AgentMiddleware):
    """Injects status updates and streams images from tools immediately."""

    # Matches any markdown image: ![alt](url)
    _IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")

    # Matches any markdown link: [text](url)
    _LINK_RE = re.compile(r"\[[^\]]+\]\([^)]+\)")

    # Tools whose function_result may contain an image to surface
    _IMAGE_TOOLS = {"get_viewport_screenshot", "render_preview", "render_final"}

    # Tools whose function_result contains a download link to surface
    _LINK_TOOLS = {"save_scene_for_download", "export_scene_as_glb_for_download"}

    _IMAGE_LABELS: dict[str, str] = {
        "render_preview": "Here's a quick preview while the final render is in progress:",
        "render_final": "Here's the rendered image:",
        "get_viewport_screenshot": "Here's the current viewport:",
    }

    async def process(self, context: AgentContext, call_next) -> None:
        await call_next()

        if not context.stream:
            return

        original_stream = context.result

        async def _wrapped():
            # Deduplicate by tool *name* so that multiple streaming chunks
            # for the same logical invocation don't produce repeated status
            # messages (the framework can emit several FunctionCallContent
            # objects with different call_ids for one invocation).
            announced_names: set[str] = set()
            # Track call_ids for image-producing tools → tool name
            image_call_ids: dict[str, str] = {}
            # Track call_ids for link-producing tools → tool name
            link_call_ids: dict[str, str] = {}
            # call_ids already surfaced early, so a re-emitted function_result
            # (the framework can send the same result in multiple chunks) is
            # never surfaced twice.
            surfaced_call_ids: set[str] = set()

            # Resilience state for this turn
            turn_started = asyncio.get_running_loop().time()
            chunks_seen = 0
            session_id = (
                getattr(context.session, "session_id", None)
                if context.session is not None
                else None
            )

            # IMPORTANT — telemetry correctness:
            # The upstream `original_stream` is the instrumented ResponseStream
            # produced by `agent_framework.observability.AgentTelemetryLayer`. It
            # owns the `invoke_agent` span and ends it from a cleanup hook
            # (`_finalize_stream`) whose `finally` calls `ContextVar.reset(token)`
            # on tokens that were `.set()` in *this* (the request handler's)
            # context. `ContextVar.reset()` is context-identity-bound, so the
            # stream MUST be iterated in this same context — NOT from a child
            # `asyncio.create_task` (which always gets a fresh Context copy). If
            # it were iterated from a background task, the reset would raise
            # `ValueError: <Token …> was created in a different Context` *before*
            # the span is ended, so the span would never be exported and the
            # App Insights "Agents (preview)" pane (which filters on
            # gen_ai.operation.name == invoke_agent) would stay empty. We
            # therefore iterate it directly here.
            #
            # Because a directly-awaited `__anext__` cannot be raced against a
            # timer without wrapping it in a task (which would re-break the
            # context), the hard turn cap is enforced by a background watchdog
            # that cancels this consumer only on a true hang. Per-tool status
            # messages (emitted inline below on each function_call) provide the
            # user-facing keep-alive during normal tool-execution gaps.
            consumer_task = asyncio.current_task()
            timed_out = False

            async def _watchdog():
                nonlocal timed_out
                try:
                    await asyncio.sleep(TURN_TIMEOUT_SECONDS)
                    timed_out = True
                    if consumer_task is not None:
                        consumer_task.cancel()
                except asyncio.CancelledError:
                    pass

            watchdog_task = asyncio.create_task(_watchdog())

            try:
                async for update in original_stream:
                    chunks_seen += 1
                    for content in (update.contents or []):
                        # ── Status messages before each tool call ──
                        if content.type == "function_call":
                            call_id = content.call_id or content.name
                            if content.name in self._IMAGE_TOOLS and call_id:
                                image_call_ids[call_id] = content.name
                            if content.name in self._LINK_TOOLS and call_id:
                                link_call_ids[call_id] = content.name
                            if content.name and content.name not in announced_names:
                                announced_names.add(content.name)
                                status = _TOOL_STATUS_MESSAGES.get(
                                    content.name, "Working on it…"
                                )
                                yield AgentResponseUpdate(
                                    contents=[Content.from_text(f"\n\n*{status}*\n\n")],
                                    role="assistant",
                                    message_id=f"status-{content.name}",
                                )

                        # ── Stream images from tool results immediately ──
                        if content.type == "function_result":
                            cid = content.call_id or ""
                            if cid in image_call_ids and cid not in surfaced_call_ids:
                                match = self._IMAGE_RE.search(
                                    content.result or ""
                                )
                                if match:
                                    surfaced_call_ids.add(cid)
                                    tool_name = image_call_ids[cid]
                                    label = self._IMAGE_LABELS.get(tool_name, "")
                                    yield AgentResponseUpdate(
                                        contents=[
                                            Content.from_text(
                                                f"\n\n{label}\n\n{match.group(0)}\n\n"
                                            )
                                        ],
                                        role="assistant",
                                        message_id=f"tool-img-{cid}",
                                    )

                            # ── Stream download links from tool results immediately ──
                            if cid in link_call_ids and cid not in surfaced_call_ids:
                                match = self._LINK_RE.search(
                                    content.result or ""
                                )
                                if match:
                                    surfaced_call_ids.add(cid)
                                    yield AgentResponseUpdate(
                                        contents=[
                                            Content.from_text(
                                                f"\n\n{match.group(0)}\n\n"
                                            )
                                        ],
                                        role="assistant",
                                        message_id=f"tool-link-{cid}",
                                    )

                    yield update
            except asyncio.CancelledError:
                if not timed_out:
                    # Genuine external cancellation — propagate untouched.
                    raise
                elapsed_ms = int(
                    (asyncio.get_running_loop().time() - turn_started) * 1000
                )
                logger.error(
                    "Agent turn timed out: session_id=%s elapsed_ms=%d "
                    "chunks_seen=%d cap_seconds=%.0f",
                    session_id, elapsed_ms, chunks_seen, TURN_TIMEOUT_SECONDS,
                )
                yield AgentResponseUpdate(
                    contents=[Content.from_text(_FRIENDLY_TIMEOUT_TEXT)],
                    role="assistant",
                    message_id="agent-turn-timeout",
                )
                # Surface as a turn timeout so server-side telemetry records the
                # failure and the framework's normal error path runs.
                raise asyncio.TimeoutError("turn wall-clock cap exceeded")
            except Exception as exc:
                elapsed_ms = int(
                    (asyncio.get_running_loop().time() - turn_started) * 1000
                )
                # Try to extract an upstream request id / status code for
                # actionable diagnostics.
                request_id = getattr(exc, "request_id", None) or getattr(
                    getattr(exc, "response", None), "headers", {}
                ).get("x-request-id") if hasattr(exc, "response") else None
                status_code = getattr(exc, "status_code", None) or getattr(
                    getattr(exc, "response", None), "status_code", None
                )
                logger.error(
                    "Agent stream failed: session_id=%s elapsed_ms=%d "
                    "chunks_seen=%d status=%s request_id=%s exc_type=%s",
                    session_id, elapsed_ms, chunks_seen,
                    status_code, request_id, type(exc).__name__,
                    exc_info=True,
                )
                # An orphaned tool call poisons the whole session — tell the user
                # (and the web client) to start fresh instead of retrying, which
                # would just replay the same broken history.
                if _is_orphaned_tool_call_error(exc):
                    logger.error(
                        "Orphaned tool call detected in session %s — history is "
                        "corrupted; client should reset the session.",
                        session_id,
                    )
                    error_text = _FRIENDLY_STATE_ERROR_TEXT
                else:
                    error_text = _FRIENDLY_MODEL_ERROR_TEXT
                yield AgentResponseUpdate(
                    contents=[Content.from_text(error_text)],
                    role="assistant",
                    message_id="agent-stream-error",
                )
                # Re-raise so server-side telemetry records the failure and
                # the framework's normal error path runs.
                raise
            finally:
                if not watchdog_task.done():
                    watchdog_task.cancel()
                try:
                    await watchdog_task
                except (asyncio.CancelledError, Exception):
                    pass

        context.result = ResponseStream(_wrapped(), finalizer=AgentResponse.from_updates)


class SceneIsolationMiddleware(AgentMiddleware):
    """Activates/saves per-conversation Blender scenes via blob storage.

    Wraps an inner middleware (ToolStatusMiddleware) so that scene
    isolation runs before/after the agent while status streaming
    continues to work.

    Key insight on thread lifecycle:
      - On the FIRST request for a new conversation, the framework creates
        a thread with service_thread_id = None.  The Azure AI service assigns
        the real ID during the streaming run, and the framework mutates the
        thread object in-place AFTER the last streaming chunk is yielded
        (via _update_thread_with_type_and_conversation_id).
      - On SUBSEQUENT requests, the thread is loaded from InMemoryAgentThread-
        Repository and already has service_thread_id set.

    Therefore:
      - ACTIVATION (before run): uses service_thread_id if available.
        On the first request it is None → we skip activation (no saved scene
        exists yet anyway — Blender starts with a clean scene).
      - SAVE (after streaming ends): reads service_thread_id from the thread
        object which has been mutated by the framework by this point, so it
        is always available.
    """

    def __init__(self, inner: AgentMiddleware, scene_manager: SceneManager):
        self._inner = inner
        self._scene_manager = scene_manager

    @staticmethod
    def _get_conversation_id(context: AgentContext) -> str | None:
        """Resolve a stable conversation/scene id for this turn.

        Lookup order (most-stable first — IMPORTANT for idle/restore
        survival; ``context.session.session_id`` is REGENERATED on every
        container restart because ``InMemoryAgentSessionRepository`` is
        wiped, so it must NOT be the primary key):

          1. Stable Foundry ``conv_xxx`` id discovered on
             ``context.session`` / ``context.agent`` (see
             ``_scan_for_stable_conv_id``). Survives idle/restore.
          2. ``context.options["user"]`` / ``context.options["metadata"]``
             — populated by the WebChat proxy from the client's
             localStorage-persisted ``conversationId``. Also stable.
          3. ``context.kwargs["user"]`` / ``context.kwargs["metadata"]``
             — same fields routed via kwargs in some runtime modes.
          4. ``agent._request_headers["conversation_id"]`` — Foundry
             adapter mirror of the body metadata.
          5. ``context.session.session_id`` — LAST resort; volatile.
          6. None — caller skips activation/save and emits a warning.
        """
        stable_val, stable_src = SceneIsolationMiddleware._scan_for_stable_conv_id(context)
        if stable_val:
            logger.info(
                "Scene isolation: resolved conversation id from %s (stable=True): %s",
                stable_src, stable_val,
            )
            return stable_val

        # Fallback 1: WebChat proxy sends `user: <conversationId>` and
        # `metadata: { conversation_id }`. These typically arrive on
        # `context.options` (request body kwargs) but the Azure runtime has
        # been observed to route them via `context.kwargs` as well, so check
        # both. Use the first non-empty hit.
        for source_name, source in (("options", context.options),
                                    ("kwargs", context.kwargs),
                                    ("metadata", context.metadata)):
            if not source:
                continue
            try:
                user_val = source.get("user")  # type: ignore[union-attr]
                if isinstance(user_val, str) and user_val:
                    logger.info(
                        "Scene isolation: resolved conversation id from %s.user",
                        source_name,
                    )
                    return user_val
                meta = source.get("metadata")  # type: ignore[union-attr]
                if isinstance(meta, dict):
                    cid = meta.get("conversation_id")
                    if isinstance(cid, str) and cid:
                        logger.info(
                            "Scene isolation: resolved conversation id from %s.metadata.conversation_id",
                            source_name,
                        )
                        return cid
            except AttributeError:
                continue

        # Fallback 2: Azure AI agentserver Foundry adapter copies the request
        # body `metadata` dict onto `agent._request_headers` (see
        # azure.ai.agentserver.agentframework._ai_agent_adapter.AgentFrameworkAIAgentAdapter.agent_run).
        # The OpenAI Responses path does NOT propagate `user`/`metadata` to
        # AgentContext.options or AgentContext.metadata, so this is the
        # primary channel in Foundry mode.
        agent = getattr(context, "agent", None)
        request_headers = getattr(agent, "_request_headers", None)
        if isinstance(request_headers, dict):
            cid = request_headers.get("conversation_id")
            if isinstance(cid, str) and cid:
                logger.info(
                    "Scene isolation: resolved conversation id from agent._request_headers.conversation_id"
                )
                return cid

        # LAST RESORT: volatile in-memory AgentSession id. This is wiped on
        # container restart, so scenes keyed by this id are orphaned on
        # idle/restore. The orphan-adoption logic in the ACTIVATE block
        # rescues them when possible.
        session = context.session
        if session is not None:
            sid = getattr(session, "session_id", None)
            if isinstance(sid, str) and sid:
                logger.warning(
                    "Scene isolation: falling back to volatile session.session_id=%s "
                    "(no stable id found — scene may be lost on idle/restore)",
                    sid,
                )
                return sid

        return None

    @staticmethod
    def _dump_context_keys_once(context: AgentContext) -> None:
        """One-shot diagnostic: log available keys on options/kwargs/metadata.

        Helps debug Foundry vs. local routing of `user`/`metadata`/etc. when
        no session id is found. Only fires the first time it is called per
        process.
        """
        if getattr(SceneIsolationMiddleware._dump_context_keys_once, "_done", False):
            return
        SceneIsolationMiddleware._dump_context_keys_once._done = True  # type: ignore[attr-defined]
        try:
            opts_keys = list(context.options.keys()) if context.options else []
            kw_keys = list(context.kwargs.keys()) if context.kwargs else []
            meta_keys = list(context.metadata.keys()) if context.metadata else []
            agent = getattr(context, "agent", None)
            req_headers = getattr(agent, "_request_headers", None)
            req_headers_keys = (
                list(req_headers.keys()) if isinstance(req_headers, dict) else f"<{type(req_headers).__name__}>"
            )
            logger.info(
                "Scene isolation diagnostic (one-shot): options keys=%s, kwargs keys=%s, "
                "metadata keys=%s, agent._request_headers=%s",
                opts_keys, kw_keys, meta_keys, req_headers_keys,
            )
        except Exception as e:  # pragma: no cover - diagnostic best-effort
            logger.warning("Scene isolation diagnostic failed: %s", e)

    @staticmethod
    def _scan_for_stable_conv_id(context: AgentContext) -> tuple[str | None, str | None]:
        """Look for a stable Foundry conversation id on context.

        The Foundry agentserver logs ``Saved agent session for conversation:
        conv_<...>`` after each turn — that id is stable across container
        restarts (it's the OpenAI-style Responses conversation id), unlike
        ``AgentSession.session_id`` which is wiped along with the
        ``InMemoryAgentSessionRepository`` on idle.

        Since the Foundry adapter is not in our codebase we don't know
        exactly which attribute holds it, so this method probes a list of
        likely locations on ``context.session`` and ``context.agent``,
        returning ``(value, source_description)`` on first hit.
        """
        # Candidate attribute names, in priority order. Anything starting
        # with ``conv_`` is by far the strongest signal.
        candidate_attrs = (
            "conversation_id",
            "conv_id",
            "service_thread_id",
            "thread_id",
            "agent_session_id",
            "external_conversation_id",
        )

        def _scan_obj(obj: object, label: str) -> tuple[str | None, str | None]:
            if obj is None:
                return (None, None)
            # 1) explicit attribute lookups
            for name in candidate_attrs:
                try:
                    val = getattr(obj, name, None)
                except Exception:
                    val = None
                if isinstance(val, str) and val:
                    return (val, f"{label}.{name}")
            # 2) scan vars()/__dict__ for any string starting with 'conv_'
            try:
                d = vars(obj)
            except TypeError:
                d = {}
            for k, v in d.items():
                if isinstance(v, str) and v.startswith("conv_"):
                    return (v, f"{label}.{k}")
            return (None, None)

        for label, target in (
            ("session", context.session),
            ("session.thread", getattr(context.session, "thread", None) if context.session else None),
            ("agent", getattr(context, "agent", None)),
        ):
            val, src = _scan_obj(target, label)
            if val:
                return (val, src)
        return (None, None)

    @staticmethod
    def _dump_stable_id_search_once(context: AgentContext) -> None:
        """Log every attribute on session/agent — used to discover where
        Foundry exposes the stable conv_xxx id when we still can't find it."""
        if getattr(SceneIsolationMiddleware._dump_stable_id_search_once, "_done", False):
            return
        SceneIsolationMiddleware._dump_stable_id_search_once._done = True  # type: ignore[attr-defined]
        try:
            for label, target in (
                ("session", context.session),
                ("agent", getattr(context, "agent", None)),
            ):
                if target is None:
                    continue
                try:
                    d = vars(target)
                except TypeError:
                    d = {}
                # Truncate values to keep log readable.
                preview = {
                    k: (repr(v)[:120] if not callable(v) else "<callable>")
                    for k, v in d.items()
                }
                logger.info(
                    "Scene isolation stable-id search: %s vars=%s, dir=%s",
                    label, preview,
                    [a for a in dir(target) if not a.startswith("_")][:40],
                )
        except Exception as e:  # pragma: no cover
            logger.warning("Scene isolation stable-id search failed: %s", e)

    async def process(self, context: AgentContext, call_next) -> None:
        session = context.session
        logger.info(
            "Scene isolation: process() called. session=%r, session_id=%r, stream=%s",
            session,
            getattr(session, "session_id", "N/A") if session else "N/A",
            context.stream,
        )

        # ── ACTIVATE (before the agent runs) ──
        conversation_id = self._get_conversation_id(context)
        if conversation_id is None:
            # First time we fail to resolve a session id, dump what's available
            # so we can diagnose how the Foundry runtime routes `user`/`metadata`.
            self._dump_context_keys_once(context)
            # Also dump full session/agent attrs to discover where Foundry
            # exposes a stable conv_xxx id (one-shot).
            self._dump_stable_id_search_once(context)

        # ── Active-vs-idle detection ──
        # A quick non-retrying socket probe tells us whether Blender is already
        # running (active mode → proceed transparently) or we are recovering
        # from idle (Blender not yet up → must wait for the supervisor and
        # inform the user via streamed status messages).
        recovery_messages: list[str] = []
        # Cold start = this is the very first boot of a fresh container.
        # Blender naturally takes ~3s to come up; we shouldn't tell the user
        # we're "recovering from idle" in that case.
        is_cold_start = os.environ.get("BLENDER_COLD_START") == "1"
        blender_ready_now = is_blender_socket_ready(timeout=1.0)
        if not blender_ready_now:
            if is_cold_start:
                logger.info(
                    "Scene isolation: Blender socket not yet reachable on cold "
                    "start — waiting for initial boot (no user-facing recovery message)"
                )
            else:
                logger.info(
                    "Scene isolation: Blender socket not reachable — recovering from idle"
                )
                recovery_messages.append(
                    "🔄 The Blender engine is restarting after being paused. "
                    "Please hold on while the supervisor brings it back up…"
                )
        else:
            logger.info("Scene isolation: Blender socket reachable — active mode")

        # ── Build user-facing status message describing what we will do with
        # the scene, BEFORE we touch it.
        # On Foundry's ADC platform each agent runs in a micro-VM bound 1:1
        # to a conversation. There is therefore at most one persisted scene
        # per container lifetime; either it exists (resume) or it doesn't
        # (fresh container).
        is_brand_new = self._scene_manager.new_scene
        has_local = self._scene_manager.has_saved_scene()
        if is_brand_new:
            recovery_messages.append(
                "🆕 First time using the agent in this session — setting up a fresh Blender scene for you."
            )
        elif has_local:
            recovery_messages.append(
                "📂 Found your previous Blender scene — restoring it now."
            )
        else:
            # Resumed container marker says we should reload, but no .blend
            # is on disk (unexpected — log already emitted by SceneManager).
            recovery_messages.append(
                "🆕 No previous scene found — starting from a clean scene."
            )

        # If we were recovering from idle, wait for Blender BEFORE attempting
        # to activate the scene (otherwise activate_scene → _load_blend_file
        # will fail and we will fall back to a clean scene, losing the user's
        # work).
        if not blender_ready_now:
            recovered = await asyncio.to_thread(_wait_for_blender, 120)
            if recovered:
                if not is_cold_start:
                    recovery_messages.append("✅ Blender is ready — loading your scene…")
                await asyncio.to_thread(self._scene_manager.set_blender_ready, True)
            else:
                recovery_messages.append(
                    "⚠️ Blender did not come back online in time. I'll try to continue, "
                    "but some tools may fail — please retry shortly if you see errors."
                )

        # Activate the scene (load persisted .blend or reset to clean).
        # conversation_id is passed for logging/diagnostics only — the
        # scene file is not keyed by it (single scene per micro-VM).
        logger.info(
            "Scene isolation: activating scene (conversation=%s)", conversation_id,
        )
        await asyncio.to_thread(self._scene_manager.activate_scene, conversation_id)

        # If Blender was already up at the start of this turn, ensure the
        # session-state file reflects that (entrypoint.sh writes blender_ready
        # =false on startup, but by the time the first request lands Blender
        # may already be up).
        if blender_ready_now:
            await asyncio.to_thread(self._scene_manager.set_blender_ready, True)

        # ── RUN the agent (inner middleware → actual LLM + tools) ──
        await self._inner.process(context, call_next)

        # ── SAVE (after the agent finishes) ──
        # For streaming responses the agent hasn't fully run yet — the stream is
        # lazily consumed.  We wrap the stream so that SAVE happens in a finally
        # block after the very last chunk.
        if context.stream:
            original_stream = context.result

            async def _save_after_stream():
                # ── Stream recovery / status messages FIRST ──
                # Surface idle-recovery and scene-restoration status to the
                # user before any model output, so they understand the wait
                # and know what the agent is doing.
                for idx, msg in enumerate(recovery_messages):
                    yield AgentResponseUpdate(
                        contents=[Content.from_text(f"\n\n*{msg}*\n\n")],
                        role="assistant",
                        message_id=f"scene-status-{idx}",
                    )
                try:
                    async for update in original_stream:
                        yield update
                finally:
                    # Resolve the conversation id (for telemetry / state-file
                    # diagnostics only — the scene file itself is no longer
                    # keyed by it). Save unconditionally: under the single-
                    # scene-per-VM model, every turn's result IS the state
                    # we need to recover from on the next idle resume.
                    save_id = self._get_conversation_id(context)
                    logger.info(
                        "Scene isolation: stream ended (conversation=%r) — saving scene",
                        save_id,
                    )
                    try:
                        await asyncio.to_thread(self._scene_manager.save_scene, save_id)
                        logger.info("Scene isolation: scene saved (conversation=%r)", save_id)
                    except Exception:
                        logger.error(
                            "Scene isolation: failed to save scene (conversation=%r)",
                            save_id, exc_info=True,
                        )

            context.result = ResponseStream(_save_after_stream(), finalizer=AgentResponse.from_updates)
        else:
            # Non-streaming: tools already ran, thread is updated.
            save_id = self._get_conversation_id(context)
            logger.info("Scene isolation: saving scene (non-streaming, conversation=%r)", save_id)
            await asyncio.to_thread(self._scene_manager.save_scene, save_id)


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────


async def main():
    """Main function to run the Blender Scene Agent as a web server."""
    scene_manager = SceneManager()

    global _scene_manager
    _scene_manager = scene_manager

    # Per the canonical 02_tools sample, `FoundryChatClient` is
    # constructed plainly (it is NOT an async context manager) and uses
    # the sync `DefaultAzureCredential`. The credential lives for the
    # lifetime of the process; explicit cleanup is unnecessary here.
    #
    # NOTE on telemetry: we add NO provider-replacing observability code here.
    # Agent Framework instrumentation is on by default and the Foundry
    # hosting layer owns the global OpenTelemetry tracer/meter providers
    # and exporters, which feed the Foundry Monitor tab AND the project-linked
    # Application Insights resource. Any manual
    # configure_azure_monitor()/set_tracer_provider() call here would
    # pre-empt the host's provider (OTel honors only the FIRST
    # set_tracer_provider call) and break that pipeline.
    credential = SyncDefaultAzureCredential()
    chat_client = FoundryChatClient(
        project_endpoint=PROJECT_ENDPOINT,
        model=MODEL_DEPLOYMENT_NAME,
        credential=credential,
    )

    # Canonical hosted-agent stack (matches
    # microsoft-foundry/foundry-samples/.../hosted-agents/agent-framework/
    # responses/02_tools/main.py):
    #
    #   FoundryChatClient + ResponsesHostServer + Agent (no name)
    #
    # We previously used `AzureAIClient` + `azure.ai.agentserver`, but
    # `AzureAIClient._prepare_options` unconditionally calls
    # `project_client.agents.create_version(...)` to register a
    # `kind: prompt` agent in the Foundry project. That is the
    # prompt-agent SDK pattern — it's incompatible with the hosted
    # agent declared in agent.yaml and causes HTTP 400
    # "Agent kind mismatch" no matter what name (or no name) is
    # passed.
    #
    # `FoundryChatClient` never calls `create_version`. The agent
    # identity lives ENTIRELY in agent.yaml (`name: fantasy-worlds-
    # agent`, `kind: hosted`), so the local `Agent(...)` wrapper has
    # NO `name=` argument — there's a single source of truth.
    #
    # `default_options={"store": False}` tells the model the service
    # manages history (per the sample). Our scene-isolation
    # middleware persists scene state out-of-band, so this is fine.
    agent = Agent(
        client=chat_client,
        middleware=[SceneIsolationMiddleware(ToolStatusMiddleware(), scene_manager)],
        default_options={"store": False},
        instructions="""You are an expert 3D scene creation assistant powered by Blender.

**CRITICAL: This environment runs Blender 4.4. All generated Python code MUST use the Blender 4.x Python API. Many Blender 3.x APIs were removed or renamed in 4.0. NEVER use deprecated Blender 3.x node types, input names, or enums — they will cause runtime errors. When uncertain about an API, prefer the Blender 4.x naming conventions listed in the "Blender 4.x API Compatibility" section below.**

## Scene state
- The Blender scene starts **empty** (no objects at all — no default cube, no camera, no light) but already has a **neutral studio HDRI environment** (studio_small_09) providing realistic hemisphere lighting.
- On the **very first turn of a brand-new conversation**, call setup_scene() as your first action to add a camera and optional sun light. Do NOT assume a default cube exists — there is none.
- Do NOT add a cube unless the user explicitly asks for one.
- **Continuation turns (CRITICAL):** Scenes are persisted per-conversation — when the user asks you to ADD to or MODIFY an existing scene ("add a sphere", "change the color", "resize…"), the previous objects are ALREADY loaded. **NEVER call setup_scene() on a continuation turn**, and NEVER call setup_scene(clear_default=True) when objects already exist — it will delete the user's work. If you are unsure whether the scene is empty, call get_scene_info() first; if it returns any non-camera/light objects, skip setup_scene entirely and just add or modify what the user asked for.

## Guidelines
- Call get_scene_info() at the start of every continuation turn (any user message after the first one in a conversation) to see what objects already exist before adding or modifying. Skip it only on the very first turn of a fresh scene.
- **Relative transforms (resize / move / rotate by a factor or offset):** `modify_object` takes ABSOLUTE values, not relative ones. When the user asks for a relative change (e.g. "reduce the size by 5x", "move it 2m to the right", "rotate 90° more"), you MUST first call `get_object_info(object_name=…)` to read the current scale/location/rotation, compute the new absolute value yourself, then pass that to `modify_object`. Calling `modify_object` with only `object_name` and no transform parameters will fail.
- Take ONE viewport screenshot at the very end after ALL changes are complete, or when the user explicitly asks. NEVER take intermediate screenshots between steps.
- For batch operations (multiple objects, materials, transforms), prefer a single execute_blender_code() call with one combined Python script instead of many sequential tool calls.
- When creating an object that needs a color, call create_object() then apply_material() — but batch multiple such pairs into one execute_blender_code() call when possible.
- Use setup_scene() to initialize camera and lighting when starting a new scene.
- Use the 3D model library (list_available_models / download_model) to add ready-made models, and Poly Haven textures (list_available_textures / apply_texture) to dress surfaces. See the "3D model library & Poly Haven textures" section below for the exact gallery workflow.
- Position objects thoughtfully — avoid overlapping, ensure proper scale (1 unit = 1 meter).
- Rotation values are in degrees.
- **Rendering workflow**:
  1. When the user asks for a high-fidelity render WITHOUT specifying a resolution (or at 640x480 or smaller): call render_final() ONCE at **640x480 with 32 samples**. Do NOT call render_preview() — a single render_final() call is sufficient. Include the render image from render_final() in your response.
  2. When the user asks for a high-fidelity render at a resolution HIGHER than 640x480 (e.g. 1920x1080): first call render_final() at **640x480 with 32 samples** as a quick preview, return that image to the user immediately, then ASK the user to confirm whether they want to generate the higher-resolution version. If confirmed, call render_final() again at the **requested resolution with 256 samples** and return that image.
- NEVER set `collection.name` — it is read-only in this Blender environment. To organize objects, create new collections with `bpy.data.collections.new("Name")` and link them to the scene with `bpy.context.scene.collection.children.link(new_collection)` instead of renaming existing ones.
- The render engine enum for Eevee in Blender 4.x is `'BLENDER_EEVEE_NEXT'`, NOT `'BLENDER_EEVEE'`. Valid engines are: `'BLENDER_EEVEE_NEXT'`, `'BLENDER_WORKBENCH'`, `'CYCLES'`.
- When a tool returns an image in markdown format (e.g. `![screenshot](url)` or `![render](url)`), you MUST include that exact markdown image link in your response so the chat client can display the image. Never omit, summarize, or rewrite the image URL.

## 3D model library & Poly Haven textures
You have two asset libraries. The web chat renders picture galleries the user can click, so you MUST follow this exact protocol.

### Ready-made 3D models (Microsoft library)
- `list_available_models(query)` — search the library. Returns a JSON array of models, each with `name`, `imageUrl` (a thumbnail) and `modelUrl`.
- `download_model(model_url, name)` — downloads a chosen GLB and imports it into the Blender scene.

Workflow:
- When the user wants to find or browse ready-made models ("find a chair", "show me some dinosaurs", "do you have a spaceship?"), call `list_available_models`. Then, in your reply, include the tool's JSON array VERBATIM inside a single fenced block tagged ` ```models ` (NOT ` ```json `). The web client renders those thumbnails as a clickable gallery. Add ONE short friendly sentence before the block, and do NOT also create objects or take a screenshot in this turn — just present the gallery.
- Example reply shape:
    Here are a few chairs I found:
    ```models
    [{"name":"Wooden Chair","imageUrl":"https://…","modelUrl":"https://….glb"}]
    ```
- When the user then picks one (by clicking a thumbnail, or saying "load the wooden one", "add the 2nd"), call `download_model` with that model's `modelUrl` and a short descriptive `name`, then take ONE viewport screenshot so they see the result.
- If `list_available_models` returns "[]", tell the user nothing matched and suggest a different search term. If it returns an "ERROR:…" string, briefly apologize and do NOT include a ` ```models ` block.

### Poly Haven PBR textures
- `list_available_textures(query)` — search Poly Haven for free surface textures (brick, wood, rock, concrete, fabric, sand, …). Returns a JSON array of textures, each with `name`, `imageUrl` (a thumbnail) and `assetId`.
- `apply_texture(asset_id, object_name, resolution)` — downloads that texture and applies it as a PBR material to a named object.

Workflow:
- When the user wants to find or browse textures ("find a brick texture", "show me some wood surfaces"), call `list_available_textures`. Then, in your reply, include the tool's JSON array VERBATIM inside a single fenced block tagged ` ```textures ` (NOT ` ```models ` and NOT ` ```json `). The web client renders those thumbnails as a clickable gallery. Add ONE short friendly sentence before the block, and do NOT apply a texture or take a screenshot in this turn — just present the gallery.
- Example reply shape:
    Here are a few brick textures I found:
    ```textures
    [{"name":"Brick Wall 006","imageUrl":"https://…","assetId":"brick_wall_006"}]
    ```
- When the user then picks one AND names an object to texture ("put the first brick on the wall", "apply that rock to the ground"), call `apply_texture` with the chosen `assetId`, the target `object_name`, and a `resolution` ('2k' unless they ask sharper/lighter), then take ONE viewport screenshot.
- If the user picks a texture but has not said which object to apply it to, ASK which object before calling `apply_texture`.
- If `list_available_textures` returns "[]", tell the user nothing matched and suggest a different search term. If it returns an "ERROR:…" string, briefly apologize and do NOT include a ` ```textures ` block.

## Recovering from Blender crashes
Some operations (especially importing large or complex 3D models, or running heavy `execute_blender_code` scripts) can occasionally crash the Blender process. When this happens, a tool will return a message that begins with **"Blender crashed"**. The previous scene has already been automatically restored from the saved state — you do NOT need to rebuild it. When you see such a message:
1. Acknowledge the crash to the user briefly (one short sentence).
2. Try a different approach for that step: a smaller resolution (e.g. `'1k'` instead of `'2k'`), a simpler asset, or build the missing geometry with `create_object` primitives.
3. Do NOT immediately retry the EXACT same operation — it is likely to crash again.
4. If the message says the circuit breaker has tripped, stop attempting Blender operations and ask the user to start a new conversation.

## Collection Management Best Practices (IMPORTANT)
`bpy.ops.mesh.primitive_*_add()` and similar operators add new objects to the **active collection**, which is NOT always `scene.collection` (the root "Scene Collection"). If you create a custom collection and link objects to it, the active collection may change — and calling `scene.collection.objects.unlink(obj)` will fail with `RuntimeError: Object 'X' not in collection 'Scene Collection'`.

**Safe helpers available in the execution namespace** (no import needed):
- `safe_move_to_collection(obj, target_collection)` — Unlinks the object from ALL its current collections, then links it to `target_collection`. Always works regardless of which collection the object is currently in.
- `safe_link_to_collection(obj, target_collection)` — Links the object to `target_collection` without removing it from other collections. Skips silently if already linked.
- `ensure_active_collection(collection)` — Sets `collection` as the active collection so that subsequent `bpy.ops.*_add()` calls place new objects directly into it. Call this once before a batch of creation operations.

**Preferred patterns:**
```python
# BEST: Set active collection first, then create objects — they go directly into it
my_col = bpy.data.collections.new('MyCollection')
bpy.context.scene.collection.children.link(my_col)
ensure_active_collection(my_col)
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube = bpy.context.active_object  # already in my_col

# ALSO GOOD: Create object, then move it with the safe helper
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube = bpy.context.active_object
safe_move_to_collection(cube, my_col)
```

**NEVER do this** (will crash if the object is not in Scene Collection):
```python
scene.collection.objects.unlink(obj)  # UNSAFE — may raise RuntimeError
target_col.objects.link(obj)
```

## Blender 4.x API Compatibility (MUST follow)
This environment runs **Blender 4.4**. The following Blender 3.x APIs were removed or renamed and MUST NOT be used:

### Removed Shader Nodes
- `ShaderNodeTexMusgrave` → REMOVED. Use `ShaderNodeTexNoise` instead. For Musgrave-like marble/terrain effects, use noise textures with appropriate scale, detail, roughness, and distortion settings.
- `ShaderNodeMixRGB` → REMOVED. Use `ShaderNodeMix` with `data_type = 'RGBA'` for color mixing. Inputs are named `A`, `B`, `Factor` (NOT `Color1`/`Color2`/`Fac`). Output is named `Result` (NOT `Color`).

### Renamed Principled BSDF Inputs (ShaderNodeBsdfPrincipled)
- `'Specular'` → use `'Specular IOR Level'`
- `'Transmission'` → use `'Transmission Weight'`
- `'Subsurface'` → use `'Subsurface Weight'`
- `'Subsurface Color'` → REMOVED (use `'Base Color'` directly)
- `'Emission'` → use `'Emission Color'`
- `'Clearcoat'` → use `'Coat'`; `'Clearcoat Roughness'` → `'Coat Roughness'`; `'Clearcoat Normal'` → `'Coat Normal'`
- `'Sheen'` → use `'Sheen Weight'`

### Render Engine Enums
- `'BLENDER_EEVEE'` → use `'BLENDER_EEVEE_NEXT'`

### Other Changes
- `ShaderNodeValToRGB` (ColorRamp): Cannot remove all elements (minimum 1 required). Modify existing elements' position and color instead of deleting and recreating.
- Never set `collection.name` (read-only). Use `bpy.data.collections.new("Name")` instead.
""",
        tools=[
            get_scene_info,
            get_object_info,
            create_object,
            modify_object,
            delete_object,
            apply_material,
            execute_blender_code,
            get_viewport_screenshot,
            list_available_models,
            download_model,
            list_available_textures,
            apply_texture,
            setup_scene,
            render_preview,
            render_final,
            save_scene_for_download,
            export_scene_as_glb_for_download,
        ],
    )
    print("Blender Scene Agent running on http://localhost:8088")
    server = ResponsesHostServer(agent)

    # Serve the text Responses API. Optionally also serve the voice WebSocket
    # (speech-in / speech-out) alongside it when Speech is configured. The voice
    # path is fully optional and isolated: if it is disabled or fails to start,
    # the text agent keeps running unaffected.
    tasks = [asyncio.ensure_future(server.run_async())]
    try:
        import voice_pipeline

        if voice_pipeline.voice_available():
            logger.info(
                "Voice path ENABLED — serving voice WebSocket on port %d.",
                voice_pipeline.VOICE_WS_PORT,
            )
            tasks.append(asyncio.ensure_future(voice_pipeline.run_ws_server(agent)))
        else:
            logger.info(
                "Voice path DISABLED (ENABLE_VOICE off or Speech not configured)."
            )
    except Exception:
        logger.warning(
            "Voice path failed to initialise; continuing with text only.",
            exc_info=True,
        )

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
