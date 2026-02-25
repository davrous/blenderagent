"""
Blender Scene Agent - An AI agent that creates and manipulates 3D scenes in Blender.
Communicates with a headless Blender instance via TCP socket (BlenderMCP protocol).
Uses Microsoft Agent Framework with Azure AI Foundry.
"""

import asyncio
import base64
import json
import os
import tempfile
from typing import Annotated

from dotenv import load_dotenv

load_dotenv(override=True)

from agent_framework.azure import AzureAIAgentClient
from azure.ai.agentserver.agentframework import from_agent_framework
from azure.identity.aio import DefaultAzureCredential

from blender_connection import get_blender_connection, close_blender_connection

# Azure AI Foundry configuration
PROJECT_ENDPOINT = os.getenv("PROJECT_ENDPOINT")
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
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_scene_info")
        return json.dumps(result)
    except Exception as e:
        return f"Error getting scene info: {str(e)}"


def get_object_info(
    object_name: Annotated[str, "The exact name of the Blender object to inspect"],
) -> str:
    """
    Get detailed information about a specific object in the Blender scene
    including its location, rotation, scale, materials, and mesh data.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_object_info", {"name": object_name})
        return json.dumps(result)
    except Exception as e:
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

    op = ops_map.get(object_type.lower())
    if not op:
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
        return f"Created {object_type} named '{name}' at ({location_x}, {location_y}, {location_z}). {result.get('result', '')}"
    except Exception as e:
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
        return f"Modified '{object_name}'. {result.get('result', '')}"
    except Exception as e:
        return f"Error modifying object: {str(e)}"


def delete_object(
    object_name: Annotated[str, "Name of the object to delete"],
) -> str:
    """Delete an object from the Blender scene by name."""
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
        return f"Deleted '{object_name}'. {result.get('result', '')}"
    except Exception as e:
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
        return f"Applied {color_hex} material to '{object_name}'. {result.get('result', '')}"
    except Exception as e:
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
    try:
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
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

        image_b64 = base64.b64encode(image_bytes).decode("utf-8")
        return f"Screenshot captured ({result.get('width', '?')}x{result.get('height', '?')} pixels). Base64 PNG data:\n\ndata:image/png;base64,{image_b64}"
    except Exception as e:
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

        return output
    except Exception as e:
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
            return message
        else:
            return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
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
    try:
        blender = get_blender_connection()
        result = blender.send_command(
            "set_texture",
            {"object_name": object_name, "texture_id": texture_id},
        )

        if "error" in result:
            return f"Error: {result['error']}"

        if result.get("success"):
            return f"Applied texture '{texture_id}' to '{object_name}'. Material: {result.get('material', '')}"
        else:
            return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
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
        return f"Scene setup complete. {result.get('result', '')}"
    except Exception as e:
        return f"Error setting up scene: {str(e)}"


def render_scene(
    output_path: Annotated[
        str, "Output file path for the render (e.g. '/tmp/render.png')"
    ] = "/tmp/render.png",
    resolution_x: Annotated[int, "Render width in pixels"] = 1920,
    resolution_y: Annotated[int, "Render height in pixels"] = 1080,
    samples: Annotated[int, "Number of render samples (higher = better quality but slower)"] = 64,
    engine: Annotated[str, "Render engine: 'CYCLES' or 'BLENDER_EEVEE_NEXT'"] = "BLENDER_EEVEE_NEXT",
) -> str:
    """
    Render the current scene to an image file using the specified render engine.
    Returns the rendered image as base64 PNG.
    EEVEE is faster, Cycles produces higher quality.
    """
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
            image_b64 = base64.b64encode(image_bytes).decode("utf-8")
            return f"Render complete ({resolution_x}x{resolution_y}). Base64 PNG:\n\ndata:image/png;base64,{image_b64}"
        else:
            return f"Render command executed but output file not found. {result.get('result', '')}"
    except Exception as e:
        return f"Error rendering: {str(e)}"


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
            instructions="""You are an expert 3D scene creation assistant powered by Blender.

## Guidelines
- Call get_scene_info() ONLY when you need to know what objects already exist (e.g. before modifying or deleting). Skip it for fresh scene creation.
- Take ONE viewport screenshot at the very end after ALL changes are complete, or when the user explicitly asks. NEVER take intermediate screenshots between steps.
- For batch operations (multiple objects, materials, transforms), prefer a single execute_blender_code() call with one combined Python script instead of many sequential tool calls.
- When creating an object that needs a color, call create_object() then apply_material() — but batch multiple such pairs into one execute_blender_code() call when possible.
- Use setup_scene() to initialize camera and lighting when starting a new scene.
- Use Poly Haven assets for high-quality textures, HDRIs, and models.
- Position objects thoughtfully — avoid overlapping, ensure proper scale (1 unit = 1 meter).
- Rotation values are in degrees. Use render_scene() for high-quality final renders.
- Poly Haven asset types: hdris (environment lighting), textures (materials), models (3D models).
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
                render_scene,
            ],
        )

        print("Blender Scene Agent running on http://localhost:8088")
        server = from_agent_framework(agent)
        await server.run_async()


if __name__ == "__main__":
    asyncio.run(main())
