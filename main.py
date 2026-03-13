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

from dotenv import load_dotenv

load_dotenv(override=True)

from agent_framework import (
    AgentMiddleware,
    AgentRunContext,
    AgentRunResponseUpdate,
    FunctionCallContent,
    FunctionResultContent,
    TextContent,
)
from agent_framework.azure import AzureAIAgentClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.ai.agentserver.agentframework.persistence import InMemoryAgentThreadRepository
from azure.identity.aio import DefaultAzureCredential

from azure.identity import DefaultAzureCredential as SyncDefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContentSettings

from blender_connection import get_blender_connection, close_blender_connection

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

# Azure AI Foundry configuration
PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")

# Azure Blob Storage configuration
AZURE_STORAGE_ACCOUNT_NAME = os.getenv("AZURE_STORAGE_ACCOUNT_NAME", "david")
BLOB_CONTAINER_NAME = "screenshots"


def upload_image_to_blob(image_bytes: bytes, blob_name: str) -> str:
    """Upload image bytes to Azure Blob Storage and return the public URL."""
    logger.info("Uploading image to blob: %s (%d bytes)", blob_name, len(image_bytes))
    account_url = f"https://{AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net"
    credential = SyncDefaultAzureCredential()
    blob_service_client = BlobServiceClient(account_url, credential=credential)
    container_client = blob_service_client.get_container_client(BLOB_CONTAINER_NAME)

    # Ensure container exists
    try:
        container_client.get_container_properties()
    except Exception:
        container_client.create_container(public_access="blob")

    blob_client = container_client.get_blob_client(blob_name)
    blob_client.upload_blob(
        image_bytes,
        overwrite=True,
        content_settings=ContentSettings(content_type="image/png"),
    )
    return f"{account_url}/{BLOB_CONTAINER_NAME}/{blob_name}"
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-4.1")


# ──────────────────────────────────────────────
# Agent tools - Scene inspection
# ──────────────────────────────────────────────


def get_scene_info() -> str:
    """
    Get detailed information about the current Blender scene including
    all objects, their types, locations, and material counts.
    Always call this first to understand the current state of the scene.
    """
    logger.info("Tool called: get_scene_info")
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
        logger.error("get_scene_info failed", exc_info=True)
        return f"Error getting scene info: {str(e)}"


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
        loc = result.get("location", {})
        if loc:
            lines.append(f"  Location: ({loc.get('x', 0)}, {loc.get('y', 0)}, {loc.get('z', 0)})")
        rot = result.get("rotation", {})
        if rot:
            lines.append(f"  Rotation: ({rot.get('x', 0)}, {rot.get('y', 0)}, {rot.get('z', 0)})")
        sc = result.get("scale", {})
        if sc:
            lines.append(f"  Scale: ({sc.get('x', 1)}, {sc.get('y', 1)}, {sc.get('z', 1)})")
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
        logger.error("execute_blender_code failed", exc_info=True)
        return f"Error executing code: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Viewport screenshot
# ──────────────────────────────────────────────


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
        temp_dir = tempfile.gettempdir()
        temp_path = os.path.join(
            temp_dir, f"blender_screenshot_{os.getpid()}.png"
        )

        result = blender.send_command(
            "get_viewport_screenshot",
            {"max_size": max_size, "filepath": temp_path, "format": "png"},
        )

        if "error" in result:
            return f"Screenshot error: {result['error']}"

        if not os.path.exists(temp_path):
            return "Error: Screenshot file was not created"

        with open(temp_path, "rb") as f:
            image_bytes = f.read()

        os.remove(temp_path)

        # Upload to Azure Blob Storage and return URL
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        blob_name = f"screenshot_{timestamp}_{uuid.uuid4().hex[:8]}.png"
        blob_url = upload_image_to_blob(image_bytes, blob_name)
        width = result.get('width', '?')
        height = result.get('height', '?')
        logger.info("get_viewport_screenshot succeeded: %sx%s, uploaded to blob", width, height)
        return f"Screenshot captured ({width}x{height} pixels).\nInclude the following image link in your response exactly as-is:\n\n![screenshot]({blob_url})"
    except Exception as e:
        logger.error("get_viewport_screenshot failed", exc_info=True)
        return f"Error capturing screenshot: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Poly Haven assets
# ──────────────────────────────────────────────


def search_polyhaven_assets(
    asset_type: Annotated[
        str,
        "Type of asset to search for: 'hdris', 'textures', 'models', or 'all'",
    ] = "all",
    categories: Annotated[
        str,
        "Optional comma-separated list of categories to filter by",
    ] = None,
) -> str:
    """
    Search for free assets on Poly Haven (HDRIs, textures, 3D models).
    Use this to find high-quality assets to enhance the scene.
    """
    logger.info("Tool called: search_polyhaven_assets(type=%r, categories=%r)", asset_type, categories)
    try:
        blender = get_blender_connection()
        result = blender.send_command(
            "search_polyhaven_assets",
            {"asset_type": asset_type, "categories": categories},
        )

        if "error" in result:
            return f"Error: {result['error']}"

        assets = result.get("assets", {})
        total = result.get("total_count", 0)
        returned = result.get("returned_count", 0)

        output = f"Found {total} assets"
        if categories:
            output += f" in categories: {categories}"
        output += f"\nShowing {returned} assets:\n\n"

        sorted_assets = sorted(
            assets.items(),
            key=lambda x: x[1].get("download_count", 0),
            reverse=True,
        )[:10]  # Limit to top 10 to reduce token usage

        for asset_id, data in sorted_assets:
            type_names = {0: "HDRI", 1: "Texture", 2: "Model"}
            output += f"- {data.get('name', asset_id)} (ID: {asset_id})"
            output += f" | {type_names.get(data.get('type', 0), 'Unknown')}"
            output += f" | {', '.join(data.get('categories', []))}\n"

        logger.info("search_polyhaven_assets succeeded: %d total assets found", total)
        return output
    except Exception as e:
        logger.error("search_polyhaven_assets failed", exc_info=True)
        return f"Error searching Poly Haven: {str(e)}"


def download_polyhaven_asset(
    asset_id: Annotated[str, "The ID of the Poly Haven asset to download"],
    asset_type: Annotated[
        str,
        "Type of asset: 'hdris', 'textures', or 'models'",
    ],
    resolution: Annotated[str, "Resolution to download (e.g. '1k', '2k', '4k')"] = "1k",
    file_format: Annotated[
        str,
        "File format: 'hdr'/'exr' for HDRIs, 'jpg'/'png' for textures, 'gltf'/'fbx' for models",
    ] = None,
) -> str:
    """
    Download and import a Poly Haven asset into the Blender scene.
    - HDRIs: Set as world environment lighting
    - Textures: Created as a material that can be applied to objects
    - Models: Imported directly into the scene
    """
    logger.info("Tool called: download_polyhaven_asset(id=%r, type=%r, res=%r, fmt=%r)",
                asset_id, asset_type, resolution, file_format)
    try:
        blender = get_blender_connection()
        result = blender.send_command(
            "download_polyhaven_asset",
            {
                "asset_id": asset_id,
                "asset_type": asset_type,
                "resolution": resolution,
                "file_format": file_format,
            },
        )

        if "error" in result:
            return f"Error: {result['error']}"

        if result.get("success"):
            message = result.get("message", "Asset imported successfully")
            if asset_type == "hdris":
                return f"{message}. The HDRI has been set as the world environment."
            elif asset_type == "textures":
                material = result.get("material", "")
                maps = ", ".join(result.get("maps", []))
                return f"{message}. Created material '{material}' with maps: {maps}."
            elif asset_type == "models":
                return f"{message}. The model has been imported into the scene."
            logger.info("download_polyhaven_asset succeeded: %s", asset_id)
            return message
        else:
            logger.warning("download_polyhaven_asset failed for %r: %s",
                           asset_id, result.get('message', 'Unknown error'))
            return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error("download_polyhaven_asset failed for %r", asset_id, exc_info=True)
        return f"Error downloading asset: {str(e)}"


def apply_polyhaven_texture(
    object_name: Annotated[str, "Name of the object to apply the texture to"],
    texture_id: Annotated[
        str, "ID of the Poly Haven texture (must be downloaded first)"
    ],
) -> str:
    """
    Apply a previously downloaded Poly Haven texture to an object.
    The texture must be downloaded first using download_polyhaven_asset.
    """
    logger.info("Tool called: apply_polyhaven_texture(object=%r, texture=%r)", object_name, texture_id)
    try:
        blender = get_blender_connection()
        result = blender.send_command(
            "set_texture",
            {"object_name": object_name, "texture_id": texture_id},
        )

        if "error" in result:
            return f"Error: {result['error']}"

        if result.get("success"):
            logger.info("apply_polyhaven_texture succeeded: %r -> %r", texture_id, object_name)
            return f"Applied texture '{texture_id}' to '{object_name}'. Material: {result.get('material', '')}"
        else:
            logger.warning("apply_polyhaven_texture failed: %s", result.get('message', 'Unknown error'))
            return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error("apply_polyhaven_texture failed for object=%r texture=%r",
                     object_name, texture_id, exc_info=True)
        return f"Error applying texture: {str(e)}"


# ──────────────────────────────────────────────
# Agent tools - Scene setup helpers
# ──────────────────────────────────────────────


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
    output_path: str,
    resolution_x: int,
    resolution_y: int,
    samples: int,
    engine: str,
) -> str:
    """Shared render helper used by render_preview and render_final."""
    logger.info("%s: rendering %dx%d, samples=%d, engine=%r",
                label, resolution_x, resolution_y, samples, engine)
    code = f"""
import bpy

bpy.context.scene.render.engine = '{engine}'
bpy.context.scene.render.resolution_x = {resolution_x}
bpy.context.scene.render.resolution_y = {resolution_y}
bpy.context.scene.render.filepath = '{output_path}'
bpy.context.scene.render.image_settings.file_format = 'PNG'

if '{engine}' == 'CYCLES':
    bpy.context.scene.cycles.samples = {samples}
    bpy.context.scene.cycles.device = 'CPU'

bpy.ops.render.render(write_still=True)
print("Render complete: {output_path}")
"""
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})

        if os.path.exists(output_path):
            with open(output_path, "rb") as f:
                image_bytes = f.read()
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            blob_name = f"{label}_{timestamp}_{uuid.uuid4().hex[:8]}.png"
            blob_url = upload_image_to_blob(image_bytes, blob_name)
            logger.info("%s succeeded: %dx%d, uploaded to blob", label, resolution_x, resolution_y)
            return f"Render complete ({resolution_x}x{resolution_y}).\nInclude the following image link in your response exactly as-is:\n\n![{label}]({blob_url})"
        else:
            logger.warning("%s: output file not found at %r", label, output_path)
            return f"Render command executed but output file not found. {result.get('result', '')}"
    except Exception as e:
        logger.error("%s failed", label, exc_info=True)
        return f"Error rendering: {str(e)}"


def render_preview(
    output_path: Annotated[
        str, "Output file path for the preview render"
    ] = "/tmp/preview.png",
    resolution_x: Annotated[int, "Preview width in pixels"] = 960,
    resolution_y: Annotated[int, "Preview height in pixels"] = 540,
    samples: Annotated[int, "Number of preview samples (keep low for speed)"] = 16,
    engine: Annotated[str, "Render engine: 'CYCLES' or 'BLENDER_EEVEE_NEXT'"] = "BLENDER_EEVEE_NEXT",
) -> str:
    """
    Fast low-resolution preview render. Use this FIRST so the user can see a
    quick result before committing to a full-quality render.
    """
    return _do_render("preview", output_path, resolution_x, resolution_y, samples, engine)


def render_final(
    output_path: Annotated[
        str, "Output file path for the final render"
    ] = "/tmp/render.png",
    resolution_x: Annotated[int, "Render width in pixels"] = 640,
    resolution_y: Annotated[int, "Render height in pixels"] = 480,
    samples: Annotated[int, "Number of render samples (higher = better quality)"] = 32,
    engine: Annotated[str, "Render engine: 'CYCLES' or 'BLENDER_EEVEE_NEXT'"] = "BLENDER_EEVEE_NEXT",
) -> str:
    """
    High-fidelity render. Default resolution is 640x480 at 32 samples.
    For higher resolutions requested by the user, first render a quick
    640x480 preview, return it, and ask the user to confirm before
    proceeding with the full resolution at 256 samples.
    """
    return _do_render("render", output_path, resolution_x, resolution_y, samples, engine)


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
    "search_polyhaven_assets": "Searching Poly Haven assets…",
    "download_polyhaven_asset": "Downloading asset from Poly Haven…",
    "apply_polyhaven_texture": "Applying Poly Haven texture…",
    "setup_scene": "Setting up the scene…",
    "render_preview": "Rendering a quick preview…",
    "render_final": "Rendering the final image (this may take a moment)…",
}


class ToolStatusMiddleware(AgentMiddleware):
    """Injects status updates and streams preview images immediately."""

    _PREVIEW_IMAGE_RE = re.compile(r"!\[preview\]\([^)]+\)")

    async def process(self, context: AgentRunContext, next) -> None:
        await next(context)

        if not context.is_streaming:
            return

        original_stream = context.result

        async def _wrapped():
            # Deduplicate by tool *name* so that multiple streaming chunks
            # for the same logical invocation don't produce repeated status
            # messages (the framework can emit several FunctionCallContent
            # objects with different call_ids for one invocation).
            announced_names: set[str] = set()
            # Track which call_ids belong to render_preview
            preview_call_ids: set[str] = set()

            async for update in original_stream:
                for content in (update.contents or []):
                    # ── Status messages before each tool call ──
                    if isinstance(content, FunctionCallContent):
                        call_id = content.call_id or content.name
                        if content.name == "render_preview" and call_id:
                            preview_call_ids.add(call_id)
                        if content.name and content.name not in announced_names:
                            announced_names.add(content.name)
                            status = _TOOL_STATUS_MESSAGES.get(
                                content.name, "Working on it…"
                            )
                            yield AgentRunResponseUpdate(
                                contents=[TextContent(text=f"\n\n*{status}*\n\n")],
                                role="assistant",
                                message_id=f"status-{content.name}",
                            )

                    # ── Stream preview image to user immediately ──
                    if isinstance(content, FunctionResultContent):
                        cid = content.call_id or ""
                        if cid in preview_call_ids:
                            match = self._PREVIEW_IMAGE_RE.search(
                                content.result or ""
                            )
                            if match:
                                yield AgentRunResponseUpdate(
                                    contents=[
                                        TextContent(
                                            text=f"\n\nHere's a quick preview while the final render is in progress:\n\n{match.group(0)}\n\n"
                                        )
                                    ],
                                    role="assistant",
                                    message_id=f"preview-img-{cid}",
                                )

                yield update

        context.result = _wrapped()


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────


async def main():
    """Main function to run the Blender Scene Agent as a web server."""
    async with (
        DefaultAzureCredential() as credential,
        AzureAIAgentClient(
            project_endpoint=PROJECT_ENDPOINT,
            model_deployment_name=MODEL_DEPLOYMENT_NAME,
            credential=credential,
        ) as client,
    ):
        agent = client.create_agent(
            name="BlenderSceneAgent",
            middleware=ToolStatusMiddleware(),
            instructions="""You are an expert 3D scene creation assistant powered by Blender.

**CRITICAL: This environment runs Blender 4.4. All generated Python code MUST use the Blender 4.x Python API. Many Blender 3.x APIs were removed or renamed in 4.0. NEVER use deprecated Blender 3.x node types, input names, or enums — they will cause runtime errors. When uncertain about an API, prefer the Blender 4.x naming conventions listed in the "Blender 4.x API Compatibility" section below.**

## Guidelines
- Call get_scene_info() ONLY when you need to know what objects already exist (e.g. before modifying or deleting). Skip it for fresh scene creation.
- Take ONE viewport screenshot at the very end after ALL changes are complete, or when the user explicitly asks. NEVER take intermediate screenshots between steps.
- For batch operations (multiple objects, materials, transforms), prefer a single execute_blender_code() call with one combined Python script instead of many sequential tool calls.
- When creating an object that needs a color, call create_object() then apply_material() — but batch multiple such pairs into one execute_blender_code() call when possible.
- Use setup_scene() to initialize camera and lighting when starting a new scene.
- Use Poly Haven assets for high-quality textures, HDRIs, and models.
- Position objects thoughtfully — avoid overlapping, ensure proper scale (1 unit = 1 meter).
- Rotation values are in degrees.
- **Rendering workflow**:
  1. When the user asks for a high-fidelity render WITHOUT specifying a resolution (or at 640x480 or smaller): call render_final() ONCE at **640x480 with 32 samples**. Do NOT call render_preview() — a single render_final() call is sufficient. Include the render image from render_final() in your response.
  2. When the user asks for a high-fidelity render at a resolution HIGHER than 640x480 (e.g. 1920x1080): first call render_final() at **640x480 with 32 samples** as a quick preview, return that image to the user immediately, then ASK the user to confirm whether they want to generate the higher-resolution version. If confirmed, call render_final() again at the **requested resolution with 256 samples** and return that image.
- Poly Haven asset types: hdris (environment lighting), textures (materials), models (3D models).
- NEVER set `collection.name` — it is read-only in this Blender environment. To organize objects, create new collections with `bpy.data.collections.new("Name")` and link them to the scene with `bpy.context.scene.collection.children.link(new_collection)` instead of renaming existing ones.
- The render engine enum for Eevee in Blender 4.x is `'BLENDER_EEVEE_NEXT'`, NOT `'BLENDER_EEVEE'`. Valid engines are: `'BLENDER_EEVEE_NEXT'`, `'BLENDER_WORKBENCH'`, `'CYCLES'`.
- When a tool returns an image in markdown format (e.g. `![screenshot](url)` or `![render](url)`), you MUST include that exact markdown image link in your response so the chat client can display the image. Never omit, summarize, or rewrite the image URL.

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
                search_polyhaven_assets,
                download_polyhaven_asset,
                apply_polyhaven_texture,
                setup_scene,
                render_preview,
                render_final,
            ],
        )

        print("Blender Scene Agent running on http://localhost:8088")
        server = from_agent_framework(agent, thread_repository=InMemoryAgentThreadRepository())
        await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
