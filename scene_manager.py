"""
Scene Manager - Per-conversation Blender scene isolation.

Each conversation gets its own isolated Blender scene state. Scenes are saved
as .blend files to Azure Blob Storage so conversations can be resumed, even
after a container restart.

Usage:
    manager = SceneManager()
    manager.activate_scene("conv-abc")   # load or reset
    # ... agent tools modify the scene ...
    manager.save_scene("conv-abc")       # persist to blob
"""

import logging
import os
import re
import tempfile

from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from blender_connection import get_blender_connection

logger = logging.getLogger("blender_agent.scene_manager")

AZURE_STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "david")
SCENE_CONTAINER_NAME = "blender-scenes"

# Temp directory for .blend files during save/load
_SCENE_DIR = os.path.join(tempfile.gettempdir(), "blender_scenes")
os.makedirs(_SCENE_DIR, exist_ok=True)

# Regex to validate conversation IDs (alphanumeric, hyphens, underscores)
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _safe_blob_name(conversation_id: str) -> str:
    """Return a safe blob name for a conversation ID."""
    if not _SAFE_ID_RE.match(conversation_id):
        # Fall back to a hash for unusual IDs
        import hashlib
        conversation_id = hashlib.sha256(conversation_id.encode()).hexdigest()[:32]
    return f"scenes/{conversation_id}.blend"


def _local_blend_path(conversation_id: str) -> str:
    """Return a local temp path for a conversation's .blend file."""
    if not _SAFE_ID_RE.match(conversation_id):
        import hashlib
        conversation_id = hashlib.sha256(conversation_id.encode()).hexdigest()[:32]
    return os.path.join(_SCENE_DIR, f"{conversation_id}.blend")


def _get_blob_container():
    """Get or create the blob container client for scene storage."""
    account_url = f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
    credential = SyncDefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url, credential=credential)
    container_client = blob_service_client.get_container_client(SCENE_CONTAINER_NAME)
    try:
        container_client.get_container_properties()
    except Exception:
        container_client.create_container()
    return container_client


class SceneManager:
    """Manages per-conversation Blender scene isolation with blob persistence."""

    def __init__(self):
        self._active_conversation_id: str | None = None

    def activate_scene(self, conversation_id: str) -> None:
        """Load the scene for the given conversation, or reset to a clean scene.

        If the same conversation is already active, this is a no-op.
        If a different conversation was active, its scene is saved first.
        """
        if conversation_id == self._active_conversation_id:
            logger.debug("Scene already active for conversation %s, skipping", conversation_id)
            return

        prev = self._active_conversation_id
        logger.info("Activating scene for conversation %s (previous: %s)",
                     conversation_id, prev)

        # Save the *previous* conversation's scene before switching
        if prev:
            logger.info("Saving previous conversation %s scene before switching", prev)
            self.save_scene(prev)

        # Try to download the saved .blend from blob storage
        local_path = _local_blend_path(conversation_id)
        blob_name = _safe_blob_name(conversation_id)
        loaded = False

        logger.info("Attempting to download scene blob: %s -> %s", blob_name, local_path)
        try:
            container = _get_blob_container()
            blob_client = container.get_blob_client(blob_name)
            with open(local_path, "wb") as f:
                stream = blob_client.download_blob()
                stream.readinto(f)
            file_size = os.path.getsize(local_path)
            logger.info("Downloaded saved scene from blob: %s (%d bytes)",
                        blob_name, file_size)
            loaded = True
        except Exception as e:
            # Blob not found or download failed — will reset to clean scene
            logger.info("No saved scene found for %s (blob=%s, error=%s), will reset to clean scene",
                        conversation_id, blob_name, e)

        if loaded:
            logger.info("Loading .blend file into Blender for conversation %s", conversation_id)
            self._load_blend_file(local_path)
        else:
            logger.info("Resetting Blender to clean scene for conversation %s", conversation_id)
            self._reset_scene()

        self._active_conversation_id = conversation_id
        logger.info("Scene activation complete for conversation %s (loaded_from_blob=%s)",
                    conversation_id, loaded)

    def save_scene(self, conversation_id: str) -> None:
        """Save the current Blender scene to blob storage for the given conversation."""
        local_path = _local_blend_path(conversation_id)
        blob_name = _safe_blob_name(conversation_id)
        logger.info("save_scene called for conversation %s (blob=%s, local=%s)",
                    conversation_id, blob_name, local_path)

        try:
            logger.info("Saving Blender scene to local .blend file: %s", local_path)
            self._save_blend_file(local_path)
            file_size = os.path.getsize(local_path)
            logger.info("Local .blend file saved: %s (%d bytes)", local_path, file_size)

            logger.info("Uploading .blend file to blob storage: %s", blob_name)
            container = _get_blob_container()
            blob_client = container.get_blob_client(blob_name)
            with open(local_path, "rb") as f:
                blob_client.upload_blob(
                    f,
                    overwrite=True,
                    content_settings=ContentSettings(
                        content_type="application/x-blender"
                    ),
                )
            logger.info("Scene uploaded to blob successfully: %s (%d bytes)", blob_name, file_size)
            # Track which conversation owns the current Blender state
            self._active_conversation_id = conversation_id
        except Exception:
            logger.error("Failed to save scene for conversation %s (blob=%s)",
                         conversation_id, blob_name, exc_info=True)

    def reset_to_clean(self) -> None:
        """Reset Blender to a clean scene and clear the active conversation."""
        logger.info("reset_to_clean: clearing active conversation %s", self._active_conversation_id)
        self._reset_scene()
        self._active_conversation_id = None

    def _load_blend_file(self, filepath: str) -> None:
        """Load a .blend file into Blender, replacing the current scene."""
        # Use forward slashes and escape backslashes for the Python string
        safe_path = filepath.replace("\\", "/")
        code = f"""
import bpy
bpy.ops.wm.open_mainfile(filepath="{safe_path}")
# Re-enable PolyHaven after file load (scene instance was replaced)
bpy.context.scene.blendermcp_use_polyhaven = True
print("Loaded scene from {safe_path}")
"""
        try:
            blender = get_blender_connection()
            blender.send_command("execute_code", {"code": code})
            logger.info("Loaded .blend file: %s", filepath)
        except Exception:
            logger.error("Failed to load .blend file %s, falling back to scene reset",
                         filepath, exc_info=True)
            self._reset_scene()

    def _save_blend_file(self, filepath: str) -> None:
        """Save the current Blender scene to a .blend file."""
        safe_path = filepath.replace("\\", "/")
        code = f"""
import bpy
bpy.ops.wm.save_as_mainfile(filepath="{safe_path}", check_existing=False)
print("Saved scene to {safe_path}")
"""
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        logger.info("Blender save command result: %s", result)
        if not os.path.exists(filepath):
            raise RuntimeError(f"Blender save command succeeded but file not found: {filepath}")
        logger.info("Saved .blend file: %s (%d bytes)", filepath, os.path.getsize(filepath))

    def _reset_scene(self) -> None:
        """Reset Blender to a completely clean scene."""
        code = """
import bpy

# Remove all objects
for obj in list(bpy.data.objects):
    bpy.data.objects.remove(obj, do_unlink=True)

# Purge orphan data (materials, meshes, textures, etc.)
for _ in range(3):
    bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)

# Reset world environment
if bpy.data.worlds:
    for world in list(bpy.data.worlds):
        bpy.data.worlds.remove(world)
world = bpy.data.worlds.new("World")
bpy.context.scene.world = world

# Re-enable PolyHaven
bpy.context.scene.blendermcp_use_polyhaven = True

print("Scene reset to clean state")
"""
        try:
            blender = get_blender_connection()
            blender.send_command("execute_code", {"code": code})
            logger.info("Scene reset to clean state")
        except Exception:
            logger.error("Failed to reset scene", exc_info=True)
