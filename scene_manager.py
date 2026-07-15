"""
Scene Manager — single-scene persistence for a Foundry-hosted Blender agent.

## Why a single scene file?

On Azure AI Foundry's ADC platform each agent runs inside an isolated
micro-VM that is bound 1:1 to a logical conversation. The platform pauses
the VM after ~60s of inactivity ("idle") and resumes it on the next
request ("active"). Resume preserves the filesystem but wipes process
memory (including the in-memory ``InMemoryAgentSessionRepository``, which
means ``AgentSession.session_id`` is regenerated even though the user
sees the same conversation).

Because one VM = one conversation, we do NOT need to disambiguate scenes
by conversation id — there is only ever ONE scene to care about per
container lifetime. The previous design used ``<conversation-id>.blend``
keys and a brittle "rename orphan after id changes" recovery path, which
is no longer required.

This module therefore stores the scene at a single fixed path:
``$HOME/blender_scenes/scene.blend``. It survives idle/resume because
``$HOME`` is persisted by the platform; it does not survive a fresh
container (intended — a fresh container is a brand-new conversation).

A small JSON state file ``$HOME/.blender_session_state`` records whether
a scene exists to be reloaded and timestamps useful for diagnostics; it
is written initially by ``entrypoint.sh`` and updated after each save.

Usage:
    manager = SceneManager()
    manager.activate_scene()   # load $HOME scene or reset to clean
    # ... agent tools modify the scene ...
    manager.save_scene()       # persist to $HOME
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from blender_connection import current_epoch, get_blender_connection

logger = logging.getLogger("blender_agent.scene_manager")

# Scene directory: $HOME/blender_scenes/ — persisted by the platform across
# idle/resume cycles. Falls back to /tmp only when $HOME is not writable
# (some local Docker setups).
_HOME_SCENE_DIR = os.path.join(os.path.expanduser("~"), "blender_scenes")
try:
    os.makedirs(_HOME_SCENE_DIR, exist_ok=True)
    _SCENE_DIR = _HOME_SCENE_DIR
    logger.info("Scene directory (primary, persisted): %s", _SCENE_DIR)
except OSError:
    _SCENE_DIR = os.path.join(tempfile.gettempdir(), "blender_scenes")
    os.makedirs(_SCENE_DIR, exist_ok=True)
    logger.warning("Could not create %s, falling back to %s", _HOME_SCENE_DIR, _SCENE_DIR)

# Fixed scene filename — one Blender scene per micro-VM/conversation.
_SCENE_FILE = os.path.join(_SCENE_DIR, "scene.blend")

# Persisted session state file (JSON). Lives in $HOME so it survives idle.
# ``entrypoint.sh`` writes the initial file (with blender_ready=false) on
# every container start; the agent updates it as Blender becomes ready and
# after every successful scene save.
_SESSION_STATE_FILE = os.path.join(os.path.expanduser("~"), ".blender_session_state")


def _read_session_state() -> dict:
    """Best-effort read of the persisted session state. Returns {} on any error."""
    try:
        with open(_SESSION_STATE_FILE) as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        logger.warning("Could not read session state file %s", _SESSION_STATE_FILE, exc_info=True)
    return {}


def _write_session_state(updates: dict) -> None:
    """Atomically merge `updates` into the session state file. Best-effort, never raises."""
    try:
        state = _read_session_state()
        state.update(updates)
        tmp = _SESSION_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, _SESSION_STATE_FILE)
    except OSError:
        logger.warning("Could not write session state file %s", _SESSION_STATE_FILE, exc_info=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class SceneManager:
    """Manages the single per-VM Blender scene with ``$HOME`` persistence."""

    def __init__(self):
        # Connection epoch captured the last time we activated (loaded or
        # reset) the scene. If the live epoch differs at save time, Blender
        # was restarted in between and the in-memory scene is NOT the one
        # the user/agent built — refuse to overwrite the saved file.
        self._active_epoch: int | None = None

        # ── newScene flag ──
        # True  → brand-new container, Blender scene should be reset to
        #         clean. The on-disk file (if any) is stale and ignored.
        # False → resumed container, prior scene exists in $HOME, load it.
        # Source: the ``needs_scene_reload`` field of the persisted state
        # file, written by ``entrypoint.sh`` based on whether the file
        # existed before this boot.
        state = _read_session_state()
        if "needs_scene_reload" in state:
            self._new_scene = not bool(state.get("needs_scene_reload"))
        else:
            # No state file → treat as brand-new container.
            self._new_scene = True
        logger.info(
            "SceneManager init: new_scene=%s (state=%r, scene_file_exists=%s)",
            self._new_scene, state, os.path.exists(_SCENE_FILE),
        )

    # ── Public read-only accessors ──

    @property
    def new_scene(self) -> bool:
        """True if this is the very first boot of the container (no scene to restore)."""
        return self._new_scene

    def has_saved_scene(self) -> bool:
        """True if ``$HOME/blender_scenes/scene.blend`` exists."""
        return os.path.exists(_SCENE_FILE)

    def is_conversation_reset(self, conversation_id: str | None) -> bool:
        """True when this turn's conversation id differs from the last saved one.

        The web-chat **Reset** button rotates the client's conversation id (and
        drops ``previous_response_id``). Because the Blender scene is persisted
        per micro-VM — NOT keyed by conversation id — a rotated id is our only
        signal that the user asked to start over, so we discard the saved scene
        instead of restoring it.

        Deliberately conservative so we never destroy a scene the user wants to
        keep: returns True only when this is NOT a brand-new container, a saved
        scene actually exists, and BOTH the incoming and last-saved conversation
        ids are known and differ. A missing/unknown id falls through to restore.
        """
        if self._new_scene or not conversation_id:
            return False
        if not os.path.exists(_SCENE_FILE):
            return False
        last = _read_session_state().get("last_conversation_id")
        return bool(last) and conversation_id != last

    # ── State-file updates ──

    def set_blender_ready(self, ready: bool) -> None:
        """Update the ``blender_ready`` flag in the persisted session state file."""
        _write_session_state({"blender_ready": bool(ready)})

    def _mark_scene_used(self) -> None:
        """After the first successful save, mark that a scene exists to be reloaded."""
        if self._new_scene:
            self._new_scene = False
        _write_session_state({"needs_scene_reload": True})

    # ── Scene lifecycle ──

    def activate_scene(self, conversation_id: str | None = None) -> None:
        """Load the persisted scene, or reset to a clean scene.

        With the single-scene/micro-VM model this is straightforward:

          * If ``new_scene`` is True (fresh container), reset to clean.
          * Else if the conversation id was rotated by the web-chat Reset
            button (``is_conversation_reset``), discard the saved scene and
            reset to clean.
          * Else if ``$HOME/blender_scenes/scene.blend`` exists, load it.
          * Else (state says we should have one but the file is missing),
            log a warning and reset to clean.

        ``conversation_id`` is used to detect a Reset (id change) and for
        logging/diagnostics.
        """
        if self._new_scene:
            logger.info(
                "activate_scene: fresh container (conversation=%s) — resetting to clean scene",
                conversation_id,
            )
            self._reset_scene()
        elif self.is_conversation_reset(conversation_id):
            last = _read_session_state().get("last_conversation_id")
            logger.info(
                "activate_scene: conversation id changed (last=%s -> now=%s) — web-chat "
                "Reset requested; discarding saved scene and starting from a clean scene",
                last, conversation_id,
            )
            self._reset_scene()
        elif os.path.exists(_SCENE_FILE):
            file_size = os.path.getsize(_SCENE_FILE)
            logger.info(
                "activate_scene: loading persisted scene (conversation=%s, file=%s, %d bytes)",
                conversation_id, _SCENE_FILE, file_size,
            )
            self._load_blend_file(_SCENE_FILE)
        else:
            logger.warning(
                "activate_scene: state says scene should reload but %s is missing "
                "(conversation=%s) — resetting to clean scene",
                _SCENE_FILE, conversation_id,
            )
            self._reset_scene()

        self._active_epoch = current_epoch()
        # Defense-in-depth: once we have activated the scene at least once
        # in this process, subsequent turns within the same active VM must
        # not be treated as "brand new" — even if a save somehow gets
        # skipped, we want the next activation to fall through to the
        # load-or-keep path rather than reset to clean.
        was_new = self._new_scene
        self._new_scene = False
        logger.info(
            "activate_scene complete (conversation=%s, was_new=%s, epoch=%d)",
            conversation_id, was_new, self._active_epoch,
        )

    def save_scene(self, conversation_id: str | None = None) -> None:
        """Save the current Blender scene to ``$HOME/blender_scenes/scene.blend``.

        ``conversation_id`` is recorded in the state file for diagnostics
        only — the scene file itself is not keyed by it.
        """
        logger.info(
            "save_scene called (conversation=%s, target=%s)",
            conversation_id, _SCENE_FILE,
        )

        # ── Crash-corruption guard ──
        # If Blender's connection epoch has bumped since we activated the
        # scene, Blender was restarted by the supervisor mid-conversation.
        # The current in-memory scene is therefore the empty default scene,
        # NOT the user's work. Refuse to overwrite the saved file.
        live_epoch = current_epoch()
        if self._active_epoch is not None and live_epoch != self._active_epoch:
            logger.error(
                "BLENDER_CRASH_DETECTED save_scene aborted: connection epoch changed "
                "(%s -> %s) since scene activation. Saved file NOT overwritten "
                "to preserve previous scene.",
                self._active_epoch, live_epoch,
            )
            return

        try:
            self._save_blend_file(_SCENE_FILE)
            file_size = os.path.getsize(_SCENE_FILE)
            logger.info("Scene saved: %s (%d bytes)", _SCENE_FILE, file_size)

            self._mark_scene_used()
            self._active_epoch = current_epoch()

            _write_session_state({
                "needs_scene_reload": True,
                "last_conversation_id": conversation_id,
                "last_saved_at": _utc_now_iso(),
            })
        except Exception:
            logger.error("Failed to save scene (conversation=%s)", conversation_id, exc_info=True)

    def reset_to_clean(self) -> None:
        """Reset Blender to a clean scene. Used by crash-recovery paths."""
        logger.info("reset_to_clean: resetting Blender scene")
        self._reset_scene()
        self._active_epoch = None

    def reload_scene(self, conversation_id: str | None = None) -> bool:
        """Force-reload the persisted scene from $HOME.

        Used to recover from a Blender process crash mid-conversation.
        Unlike ``activate_scene()`` this bypasses the ``new_scene`` flag and
        always tries the on-disk file first. Falls back to a clean reset if
        the file is missing.

        Returns True if a saved scene was loaded, False otherwise.
        """
        loaded = False
        if os.path.exists(_SCENE_FILE):
            file_size = os.path.getsize(_SCENE_FILE)
            logger.info(
                "reload_scene: loading %s (%d bytes, conversation=%s)",
                _SCENE_FILE, file_size, conversation_id,
            )
            self._load_blend_file(_SCENE_FILE)
            loaded = True
        else:
            logger.warning(
                "reload_scene: no saved scene at %s (conversation=%s) — resetting",
                _SCENE_FILE, conversation_id,
            )
            self._reset_scene()

        self._active_epoch = current_epoch()
        logger.info(
            "reload_scene complete (conversation=%s, loaded=%s, epoch=%d)",
            conversation_id, loaded, self._active_epoch,
        )
        return loaded

    # ── Blender-side operations ──

    def _load_blend_file(self, filepath: str) -> None:
        """Load a .blend file into Blender, replacing the current scene."""
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
            logger.error(
                "Failed to load .blend file %s, falling back to scene reset",
                filepath, exc_info=True,
            )
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

            # Download a neutral studio HDRI for default hemisphere lighting
            logger.info("Downloading default HDRI (studio_small_09) for scene lighting")
            blender.send_command(
                "download_polyhaven_asset",
                {"asset_id": "studio_small_09", "asset_type": "hdris", "resolution": "1k"},
            )
            logger.info("Default HDRI applied successfully")
        except Exception:
            logger.error("Failed to reset scene", exc_info=True)
