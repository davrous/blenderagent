"""
Blender Headless Startup Script
Adapted from blender-mcp addon.py (https://github.com/ahujasid/blender-mcp)
Runs the BlenderMCP socket server inside Blender without any UI components.
Designed for headless/Docker use with xvfb.
"""

import re
import bpy
import mathutils
import json
import threading
import socket
import time
import requests
import tempfile
import traceback
import os
import shutil
import zipfile
import io
from datetime import datetime
from contextlib import redirect_stdout, suppress
import logging

# Configure logger for BlenderMCP server
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
_logger = logging.getLogger("BlenderMCP")

# Add User-Agent as required by Poly Haven API
REQ_HEADERS = requests.utils.default_headers()
REQ_HEADERS.update({"User-Agent": "blender-mcp"})


class BlenderMCPServer:
    def __init__(self, host="localhost", port=9876):
        self.host = host
        self.port = port
        self.running = False
        self.socket = None
        self.server_thread = None

    def start(self):
        if self.running:
            print("Server is already running")
            return

        self.running = True

        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.host, self.port))
            self.socket.listen(1)

            self.server_thread = threading.Thread(target=self._server_loop)
            self.server_thread.daemon = True
            self.server_thread.start()

            print(f"BlenderMCP server started on {self.host}:{self.port}")
        except Exception as e:
            _logger.error(f"Failed to start server: {str(e)}")
            self.stop()

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
            self.socket = None

        if self.server_thread:
            try:
                if self.server_thread.is_alive():
                    self.server_thread.join(timeout=1.0)
            except:
                pass
            self.server_thread = None

        print("BlenderMCP server stopped")

    def _server_loop(self):
        """Main server loop in a separate thread"""
        _logger.debug("Server thread started")
        self.socket.settimeout(1.0)

        while self.running:
            try:
                try:
                    client, address = self.socket.accept()
                    _logger.debug(f"Connected to client: {address}")
                    client_thread = threading.Thread(
                        target=self._handle_client,
                        args=(client,),
                    )
                    client_thread.daemon = True
                    client_thread.start()
                except socket.timeout:
                    continue
                except Exception as e:
                    _logger.error(f"Error accepting connection: {str(e)}")
                    time.sleep(0.5)
            except Exception as e:
                _logger.error(f"Error in server loop: {str(e)}")
                if not self.running:
                    break
                time.sleep(0.5)

        _logger.debug("Server thread stopped")

    def _handle_client(self, client):
        """Handle connected client"""
        _logger.debug("Client handler started")
        client.settimeout(None)
        buffer = b""

        try:
            while self.running:
                try:
                    data = client.recv(8192)
                    if not data:
                        _logger.debug("Client disconnected")
                        break

                    buffer += data
                    try:
                        command = json.loads(buffer.decode("utf-8"))
                        buffer = b""

                        def execute_wrapper():
                            try:
                                response = self.execute_command(command)
                                response_json = json.dumps(response)
                                try:
                                    client.sendall(
                                        response_json.encode("utf-8")
                                    )
                                except:
                                    _logger.warning(
                                        "Failed to send response - client disconnected"
                                    )
                            except Exception as e:
                                _logger.error(f"Error executing command: {str(e)}")
                                traceback.print_exc()
                                try:
                                    error_response = {
                                        "status": "error",
                                        "message": str(e),
                                    }
                                    client.sendall(
                                        json.dumps(error_response).encode(
                                            "utf-8"
                                        )
                                    )
                                except:
                                    pass
                            return None

                        # Schedule execution in Blender's main thread
                        bpy.app.timers.register(
                            execute_wrapper, first_interval=0.0
                        )
                    except json.JSONDecodeError:
                        # Incomplete data, wait for more
                        pass
                except Exception as e:
                    _logger.error(f"Error receiving data: {str(e)}")
                    break
        except Exception as e:
            _logger.error(f"Error in client handler: {str(e)}")
        finally:
            try:
                client.close()
            except:
                pass
            print("Client handler stopped")

    def execute_command(self, command):
        """Execute a command in the main Blender thread"""
        try:
            return self._execute_command_internal(command)
        except Exception as e:
            _logger.error(f"Error executing command: {str(e)}")
            traceback.print_exc()
            return {"status": "error", "message": str(e)}

    def _execute_command_internal(self, command):
        """Internal command execution with proper context"""
        cmd_type = command.get("type")
        params = command.get("params", {})

        if cmd_type == "get_polyhaven_status":
            return {
                "status": "success",
                "result": self.get_polyhaven_status(),
            }

        handlers = {
            "get_scene_info": self.get_scene_info,
            "get_object_info": self.get_object_info,
            "get_viewport_screenshot": self.get_viewport_screenshot,
            "execute_code": self.execute_code,
            "get_polyhaven_status": self.get_polyhaven_status,
        }

        # Add Polyhaven handlers if enabled
        if bpy.context.scene.blendermcp_use_polyhaven:
            polyhaven_handlers = {
                "get_polyhaven_categories": self.get_polyhaven_categories,
                "search_polyhaven_assets": self.search_polyhaven_assets,
                "download_polyhaven_asset": self.download_polyhaven_asset,
                "set_texture": self.set_texture,
            }
            handlers.update(polyhaven_handlers)

        handler = handlers.get(cmd_type)
        if handler:
            try:
                _logger.debug(f"Executing handler for {cmd_type}")
                result = handler(**params)
                _logger.debug("Handler execution complete")
                return {"status": "success", "result": result}
            except Exception as e:
                _logger.error(f"Error in handler: {str(e)}")
                traceback.print_exc()
                return {"status": "error", "message": str(e)}
        else:
            return {
                "status": "error",
                "message": f"Unknown command type: {cmd_type}",
            }

    # ──────────────────────────────────────────────
    # Core command handlers
    # ──────────────────────────────────────────────

    def get_scene_info(self):
        """Get information about the current Blender scene"""
        try:
            _logger.debug("Getting scene info...")
            scene_info = {
                "name": bpy.context.scene.name,
                "object_count": len(bpy.context.scene.objects),
                "objects": [],
                "materials_count": len(bpy.data.materials),
            }

            for i, obj in enumerate(bpy.context.scene.objects):
                if i >= 20:
                    break
                obj_info = {
                    "name": obj.name,
                    "type": obj.type,
                    "location": [
                        round(float(obj.location.x), 2),
                        round(float(obj.location.y), 2),
                        round(float(obj.location.z), 2),
                    ],
                }
                scene_info["objects"].append(obj_info)

            _logger.debug(
                f"Scene info collected: {len(scene_info['objects'])} objects"
            )
            return scene_info
        except Exception as e:
            _logger.error(f"Error in get_scene_info: {str(e)}")
            traceback.print_exc()
            return {"error": str(e)}

    @staticmethod
    def _get_aabb(obj):
        """Returns the world-space axis-aligned bounding box (AABB) of an object."""
        if obj.type != "MESH":
            raise TypeError("Object must be a mesh")
        local_bbox_corners = [
            mathutils.Vector(corner) for corner in obj.bound_box
        ]
        world_bbox_corners = [
            obj.matrix_world @ corner for corner in local_bbox_corners
        ]
        min_corner = mathutils.Vector(map(min, zip(*world_bbox_corners)))
        max_corner = mathutils.Vector(map(max, zip(*world_bbox_corners)))
        return [[*min_corner], [*max_corner]]

    def get_object_info(self, name):
        """Get detailed information about a specific object"""
        obj = bpy.data.objects.get(name)
        if not obj:
            raise ValueError(f"Object not found: {name}")

        obj_info = {
            "name": obj.name,
            "type": obj.type,
            "location": [obj.location.x, obj.location.y, obj.location.z],
            "rotation": [
                obj.rotation_euler.x,
                obj.rotation_euler.y,
                obj.rotation_euler.z,
            ],
            "scale": [obj.scale.x, obj.scale.y, obj.scale.z],
            "visible": obj.visible_get(),
            "materials": [],
        }

        if obj.type == "MESH":
            bounding_box = self._get_aabb(obj)
            obj_info["world_bounding_box"] = bounding_box

        for slot in obj.material_slots:
            if slot.material:
                obj_info["materials"].append(slot.material.name)

        if obj.type == "MESH" and obj.data:
            mesh = obj.data
            obj_info["mesh"] = {
                "vertices": len(mesh.vertices),
                "edges": len(mesh.edges),
                "polygons": len(mesh.polygons),
            }

        return obj_info

    def get_viewport_screenshot(self, max_size=800, filepath=None, format="png"):
        """Capture a screenshot of the current 3D viewport using OpenGL render."""
        try:
            if not filepath:
                return {"error": "No filepath provided"}

            scene = bpy.context.scene

            # Save existing render output settings
            orig_filepath = scene.render.filepath
            orig_format = scene.render.image_settings.file_format
            orig_color_mode = scene.render.image_settings.color_mode
            orig_res_x = scene.render.resolution_x
            orig_res_y = scene.render.resolution_y
            orig_percentage = scene.render.resolution_percentage

            try:
                # Configure render output
                scene.render.filepath = filepath
                scene.render.image_settings.file_format = format.upper()
                scene.render.image_settings.color_mode = "RGB"
                scene.render.resolution_percentage = 100

                # Render the viewport using OpenGL (reads from the Blender display buffer)
                bpy.ops.render.opengl(write_still=True)

            finally:
                # Restore original settings
                scene.render.filepath = orig_filepath
                scene.render.image_settings.file_format = orig_format
                scene.render.image_settings.color_mode = orig_color_mode
                scene.render.resolution_x = orig_res_x
                scene.render.resolution_y = orig_res_y
                scene.render.resolution_percentage = orig_percentage

            # Load saved image to get dimensions and optionally scale it
            img = bpy.data.images.load(filepath)
            width, height = img.size

            if max(width, height) > max_size:
                scale = max_size / max(width, height)
                new_width = int(width * scale)
                new_height = int(height * scale)
                img.scale(new_width, new_height)
                img.file_format = format.upper()
                img.save()
                width, height = new_width, new_height

            bpy.data.images.remove(img)

            return {
                "success": True,
                "width": width,
                "height": height,
                "filepath": filepath,
            }
        except Exception as e:
            return {"error": str(e)}

    def execute_code(self, code):
        """Execute arbitrary Blender Python code"""
        try:
            namespace = {"bpy": bpy}
            capture_buffer = io.StringIO()
            with redirect_stdout(capture_buffer):
                exec(code, namespace)
            captured_output = capture_buffer.getvalue()
            return {"executed": True, "result": captured_output}
        except AttributeError as e:
            error_msg = str(e)
            if "read-only" in error_msg:
                return {
                    "executed": False,
                    "error": f"Read-only attribute error: {error_msg}. "
                    "Hint: Collection.name is read-only. Use bpy.data.collections.new('Name') instead of renaming.",
                }
            raise Exception(f"Code execution error: {error_msg}")
        except Exception as e:
            raise Exception(f"Code execution error: {str(e)}")

    # ──────────────────────────────────────────────
    # Poly Haven integration
    # ──────────────────────────────────────────────

    def get_polyhaven_status(self):
        """Get the current status of PolyHaven integration"""
        enabled = bpy.context.scene.blendermcp_use_polyhaven
        if enabled:
            return {
                "enabled": True,
                "message": "PolyHaven integration is enabled and ready to use.",
            }
        else:
            return {
                "enabled": False,
                "message": "PolyHaven integration is currently disabled.",
            }

    def get_polyhaven_categories(self, asset_type):
        """Get categories for a specific asset type from Polyhaven"""
        try:
            if asset_type not in ["hdris", "textures", "models", "all"]:
                return {
                    "error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"
                }
            response = requests.get(
                f"https://api.polyhaven.com/categories/{asset_type}",
                headers=REQ_HEADERS,
            )
            if response.status_code == 200:
                return {"categories": response.json()}
            else:
                return {
                    "error": f"API request failed with status code {response.status_code}"
                }
        except Exception as e:
            return {"error": str(e)}

    def search_polyhaven_assets(self, asset_type=None, categories=None):
        """Search for assets from Polyhaven with optional filtering"""
        try:
            url = "https://api.polyhaven.com/assets"
            params = {}
            if asset_type and asset_type != "all":
                if asset_type not in ["hdris", "textures", "models"]:
                    return {
                        "error": f"Invalid asset type: {asset_type}. Must be one of: hdris, textures, models, all"
                    }
                params["type"] = asset_type
            if categories:
                params["categories"] = categories

            response = requests.get(url, params=params, headers=REQ_HEADERS)
            if response.status_code == 200:
                assets = response.json()
                limited_assets = {}
                for i, (key, value) in enumerate(assets.items()):
                    if i >= 20:
                        break
                    limited_assets[key] = value
                return {
                    "assets": limited_assets,
                    "total_count": len(assets),
                    "returned_count": len(limited_assets),
                }
            else:
                return {
                    "error": f"API request failed with status code {response.status_code}"
                }
        except Exception as e:
            return {"error": str(e)}

    def download_polyhaven_asset(
        self, asset_id, asset_type, resolution="1k", file_format=None
    ):
        """Download and import a Poly Haven asset into Blender"""
        try:
            files_response = requests.get(
                f"https://api.polyhaven.com/files/{asset_id}",
                headers=REQ_HEADERS,
            )
            if files_response.status_code != 200:
                return {
                    "error": f"Failed to get asset files: {files_response.status_code}"
                }

            files_data = files_response.json()

            if asset_type == "hdris":
                return self._download_hdri(
                    asset_id, files_data, resolution, file_format
                )
            elif asset_type == "textures":
                return self._download_texture(
                    asset_id, files_data, resolution, file_format
                )
            elif asset_type == "models":
                return self._download_model(
                    asset_id, files_data, resolution, file_format
                )
            else:
                return {"error": f"Unsupported asset type: {asset_type}"}

        except Exception as e:
            return {"error": f"Failed to download asset: {str(e)}"}

    def _download_hdri(self, asset_id, files_data, resolution, file_format):
        """Download and apply an HDRI from Poly Haven"""
        if not file_format:
            file_format = "hdr"

        if (
            "hdri" in files_data
            and resolution in files_data["hdri"]
            and file_format in files_data["hdri"][resolution]
        ):
            file_info = files_data["hdri"][resolution][file_format]
            file_url = file_info["url"]

            with tempfile.NamedTemporaryFile(
                suffix=f".{file_format}", delete=False
            ) as tmp_file:
                response = requests.get(file_url, headers=REQ_HEADERS)
                if response.status_code != 200:
                    return {
                        "error": f"Failed to download HDRI: {response.status_code}"
                    }
                tmp_file.write(response.content)
                tmp_path = tmp_file.name

            try:
                if not bpy.data.worlds:
                    bpy.data.worlds.new("World")

                world = bpy.data.worlds[0]
                world.use_nodes = True
                node_tree = world.node_tree

                for node in node_tree.nodes:
                    node_tree.nodes.remove(node)

                tex_coord = node_tree.nodes.new(type="ShaderNodeTexCoord")
                tex_coord.location = (-800, 0)
                mapping = node_tree.nodes.new(type="ShaderNodeMapping")
                mapping.location = (-600, 0)
                env_tex = node_tree.nodes.new(type="ShaderNodeTexEnvironment")
                env_tex.location = (-400, 0)
                env_tex.image = bpy.data.images.load(tmp_path)

                if file_format.lower() == "exr":
                    try:
                        env_tex.image.colorspace_settings.name = "Linear"
                    except:
                        env_tex.image.colorspace_settings.name = "Non-Color"
                else:
                    for cs in ["Linear", "Linear Rec.709", "Non-Color"]:
                        try:
                            env_tex.image.colorspace_settings.name = cs
                            break
                        except:
                            continue

                background = node_tree.nodes.new(type="ShaderNodeBackground")
                background.location = (-200, 0)
                output = node_tree.nodes.new(type="ShaderNodeOutputWorld")
                output.location = (0, 0)

                node_tree.links.new(
                    tex_coord.outputs["Generated"], mapping.inputs["Vector"]
                )
                node_tree.links.new(
                    mapping.outputs["Vector"], env_tex.inputs["Vector"]
                )
                node_tree.links.new(
                    env_tex.outputs["Color"], background.inputs["Color"]
                )
                node_tree.links.new(
                    background.outputs["Background"], output.inputs["Surface"]
                )

                bpy.context.scene.world = world

                return {
                    "success": True,
                    "message": f"HDRI {asset_id} imported successfully",
                    "image_name": env_tex.image.name,
                }
            except Exception as e:
                return {
                    "error": f"Failed to set up HDRI in Blender: {str(e)}"
                }
        else:
            return {
                "error": "Requested resolution or format not available for this HDRI"
            }

    def _download_texture(self, asset_id, files_data, resolution, file_format):
        """Download and create a material from a Poly Haven texture"""
        if not file_format:
            file_format = "jpg"

        downloaded_maps = {}
        try:
            for map_type in files_data:
                if map_type not in ["blend", "gltf"]:
                    if (
                        resolution in files_data[map_type]
                        and file_format in files_data[map_type][resolution]
                    ):
                        file_info = files_data[map_type][resolution][
                            file_format
                        ]
                        file_url = file_info["url"]

                        with tempfile.NamedTemporaryFile(
                            suffix=f".{file_format}", delete=False
                        ) as tmp_file:
                            response = requests.get(
                                file_url, headers=REQ_HEADERS
                            )
                            if response.status_code == 200:
                                tmp_file.write(response.content)
                                tmp_path = tmp_file.name
                                image = bpy.data.images.load(tmp_path)
                                image.name = (
                                    f"{asset_id}_{map_type}.{file_format}"
                                )
                                image.pack()

                                if map_type in [
                                    "color",
                                    "diffuse",
                                    "albedo",
                                ]:
                                    try:
                                        image.colorspace_settings.name = (
                                            "sRGB"
                                        )
                                    except:
                                        pass
                                else:
                                    try:
                                        image.colorspace_settings.name = (
                                            "Non-Color"
                                        )
                                    except:
                                        pass

                                downloaded_maps[map_type] = image
                                try:
                                    os.unlink(tmp_path)
                                except:
                                    pass

            if not downloaded_maps:
                return {
                    "error": "No texture maps found for the requested resolution and format"
                }

            mat = bpy.data.materials.new(name=asset_id)
            mat.use_nodes = True
            nodes = mat.node_tree.nodes
            links = mat.node_tree.links

            for node in nodes:
                nodes.remove(node)

            output = nodes.new(type="ShaderNodeOutputMaterial")
            output.location = (300, 0)
            principled = nodes.new(type="ShaderNodeBsdfPrincipled")
            principled.location = (0, 0)
            links.new(principled.outputs[0], output.inputs[0])

            tex_coord = nodes.new(type="ShaderNodeTexCoord")
            tex_coord.location = (-800, 0)
            mapping = nodes.new(type="ShaderNodeMapping")
            mapping.location = (-600, 0)
            mapping.vector_type = "TEXTURE"
            links.new(tex_coord.outputs["UV"], mapping.inputs["Vector"])

            x_pos = -400
            y_pos = 300

            for map_type, image in downloaded_maps.items():
                tex_node = nodes.new(type="ShaderNodeTexImage")
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image
                links.new(mapping.outputs["Vector"], tex_node.inputs["Vector"])

                if map_type.lower() in ["color", "diffuse", "albedo"]:
                    links.new(
                        tex_node.outputs["Color"],
                        principled.inputs["Base Color"],
                    )
                elif map_type.lower() in ["roughness", "rough"]:
                    links.new(
                        tex_node.outputs["Color"],
                        principled.inputs["Roughness"],
                    )
                elif map_type.lower() in ["metallic", "metalness", "metal"]:
                    links.new(
                        tex_node.outputs["Color"],
                        principled.inputs["Metallic"],
                    )
                elif map_type.lower() in ["normal", "nor"]:
                    normal_map = nodes.new(type="ShaderNodeNormalMap")
                    normal_map.location = (x_pos + 200, y_pos)
                    links.new(
                        tex_node.outputs["Color"],
                        normal_map.inputs["Color"],
                    )
                    links.new(
                        normal_map.outputs["Normal"],
                        principled.inputs["Normal"],
                    )
                elif map_type in ["displacement", "disp", "height"]:
                    disp_node = nodes.new(type="ShaderNodeDisplacement")
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    links.new(
                        tex_node.outputs["Color"],
                        disp_node.inputs["Height"],
                    )
                    links.new(
                        disp_node.outputs["Displacement"],
                        output.inputs["Displacement"],
                    )

                y_pos -= 250

            return {
                "success": True,
                "message": f"Texture {asset_id} imported as material",
                "material": mat.name,
                "maps": list(downloaded_maps.keys()),
            }
        except Exception as e:
            return {"error": f"Failed to process textures: {str(e)}"}

    def _download_model(self, asset_id, files_data, resolution, file_format):
        """Download and import a Poly Haven model"""
        if not file_format:
            file_format = "gltf"

        if (
            file_format in files_data
            and resolution in files_data[file_format]
        ):
            file_info = files_data[file_format][resolution][file_format]
            file_url = file_info["url"]
            temp_dir = tempfile.mkdtemp()
            main_file_path = ""

            try:
                main_file_name = file_url.split("/")[-1]
                main_file_path = os.path.join(temp_dir, main_file_name)

                response = requests.get(file_url, headers=REQ_HEADERS)
                if response.status_code != 200:
                    return {
                        "error": f"Failed to download model: {response.status_code}"
                    }

                with open(main_file_path, "wb") as f:
                    f.write(response.content)

                # Download included files
                if "include" in file_info and file_info["include"]:
                    for include_path, include_info in file_info[
                        "include"
                    ].items():
                        include_url = include_info["url"]
                        include_file_path = os.path.join(
                            temp_dir, include_path
                        )
                        os.makedirs(
                            os.path.dirname(include_file_path), exist_ok=True
                        )
                        include_response = requests.get(
                            include_url, headers=REQ_HEADERS
                        )
                        if include_response.status_code == 200:
                            with open(include_file_path, "wb") as f:
                                f.write(include_response.content)

                # Import based on format
                if file_format in ["gltf", "glb"]:
                    bpy.ops.import_scene.gltf(filepath=main_file_path)
                elif file_format == "fbx":
                    bpy.ops.import_scene.fbx(filepath=main_file_path)
                elif file_format == "obj":
                    bpy.ops.import_scene.obj(filepath=main_file_path)

                imported_objects = [
                    obj.name for obj in bpy.context.selected_objects
                ]
                return {
                    "success": True,
                    "message": f"Model {asset_id} imported successfully",
                    "imported_objects": imported_objects,
                }
            except Exception as e:
                return {"error": f"Failed to import model: {str(e)}"}
            finally:
                with suppress(Exception):
                    shutil.rmtree(temp_dir)
        else:
            return {
                "error": "Requested format or resolution not available for this model"
            }

    def set_texture(self, object_name, texture_id):
        """Apply a previously downloaded Polyhaven texture to an object"""
        try:
            obj = bpy.data.objects.get(object_name)
            if not obj:
                return {"error": f"Object not found: {object_name}"}

            if not hasattr(obj, "data") or not hasattr(
                obj.data, "materials"
            ):
                return {
                    "error": f"Object {object_name} cannot accept materials"
                }

            # Find texture images
            texture_images = {}
            for img in bpy.data.images:
                if img.name.startswith(texture_id + "_"):
                    map_type = img.name.split("_")[-1].split(".")[0]
                    img.reload()
                    if map_type.lower() in ["color", "diffuse", "albedo"]:
                        try:
                            img.colorspace_settings.name = "sRGB"
                        except:
                            pass
                    else:
                        try:
                            img.colorspace_settings.name = "Non-Color"
                        except:
                            pass
                    if not img.packed_file:
                        img.pack()
                    texture_images[map_type] = img

            if not texture_images:
                return {
                    "error": f"No texture images found for: {texture_id}. Please download the texture first."
                }

            # Create material
            new_mat_name = f"{texture_id}_material_{object_name}"
            existing_mat = bpy.data.materials.get(new_mat_name)
            if existing_mat:
                bpy.data.materials.remove(existing_mat)

            new_mat = bpy.data.materials.new(name=new_mat_name)
            new_mat.use_nodes = True
            nodes = new_mat.node_tree.nodes
            links = new_mat.node_tree.links
            nodes.clear()

            output = nodes.new(type="ShaderNodeOutputMaterial")
            output.location = (600, 0)
            principled = nodes.new(type="ShaderNodeBsdfPrincipled")
            principled.location = (300, 0)
            links.new(principled.outputs[0], output.inputs[0])

            tex_coord = nodes.new(type="ShaderNodeTexCoord")
            tex_coord.location = (-800, 0)
            mapping = nodes.new(type="ShaderNodeMapping")
            mapping.location = (-600, 0)
            mapping.vector_type = "TEXTURE"
            links.new(tex_coord.outputs["UV"], mapping.inputs["Vector"])

            x_pos = -400
            y_pos = 300

            for map_type, image in texture_images.items():
                tex_node = nodes.new(type="ShaderNodeTexImage")
                tex_node.location = (x_pos, y_pos)
                tex_node.image = image
                links.new(mapping.outputs["Vector"], tex_node.inputs["Vector"])

                if map_type.lower() in ["color", "diffuse", "albedo"]:
                    links.new(
                        tex_node.outputs["Color"],
                        principled.inputs["Base Color"],
                    )
                elif map_type.lower() in ["roughness", "rough"]:
                    links.new(
                        tex_node.outputs["Color"],
                        principled.inputs["Roughness"],
                    )
                elif map_type.lower() in ["metallic", "metalness", "metal"]:
                    links.new(
                        tex_node.outputs["Color"],
                        principled.inputs["Metallic"],
                    )
                elif map_type.lower() in ["normal", "nor", "dx", "gl"]:
                    normal_map_node = nodes.new(type="ShaderNodeNormalMap")
                    normal_map_node.location = (x_pos + 200, y_pos)
                    links.new(
                        tex_node.outputs["Color"],
                        normal_map_node.inputs["Color"],
                    )
                    links.new(
                        normal_map_node.outputs["Normal"],
                        principled.inputs["Normal"],
                    )
                elif map_type.lower() in [
                    "displacement",
                    "disp",
                    "height",
                ]:
                    disp_node = nodes.new(type="ShaderNodeDisplacement")
                    disp_node.location = (x_pos + 200, y_pos - 200)
                    disp_node.inputs["Scale"].default_value = 0.1
                    links.new(
                        tex_node.outputs["Color"],
                        disp_node.inputs["Height"],
                    )
                    links.new(
                        disp_node.outputs["Displacement"],
                        output.inputs["Displacement"],
                    )

                y_pos -= 250

            # Clear and assign material
            while len(obj.data.materials) > 0:
                obj.data.materials.pop(index=0)
            obj.data.materials.append(new_mat)
            bpy.context.view_layer.objects.active = obj
            obj.select_set(True)
            bpy.context.view_layer.update()

            return {
                "success": True,
                "message": f"Applied texture {texture_id} to {object_name}",
                "material": new_mat.name,
                "maps": list(texture_images.keys()),
            }
        except Exception as e:
            _logger.error(f"Error in set_texture: {str(e)}")
            traceback.print_exc()
            return {"error": f"Failed to apply texture: {str(e)}"}


# ──────────────────────────────────────────────
# Headless registration (no UI panels/operators)
# ──────────────────────────────────────────────


def register_properties():
    """Register scene properties needed by the server (no UI)"""
    bpy.types.Scene.blendermcp_port = bpy.props.IntProperty(
        name="Port",
        description="Port for the BlenderMCP server",
        default=9876,
        min=1024,
        max=65535,
    )
    bpy.types.Scene.blendermcp_server_running = bpy.props.BoolProperty(
        name="Server Running", default=False
    )
    bpy.types.Scene.blendermcp_use_polyhaven = bpy.props.BoolProperty(
        name="Use Poly Haven",
        description="Enable Poly Haven asset integration",
        default=True,  # Enable by default in headless mode
    )


def start_server_headless():
    """Start the BlenderMCP server automatically in headless mode"""
    port = int(os.environ.get("BLENDER_PORT", "9876"))

    # Set Poly Haven enabled by default
    bpy.context.scene.blendermcp_use_polyhaven = True

    server = BlenderMCPServer(host="0.0.0.0", port=port)
    server.start()
    bpy.types.blendermcp_server = server
    bpy.context.scene.blendermcp_server_running = True

    print(f"BlenderMCP headless server started on port {port}")
    return server


# ──────────────────────────────────────────────
# Main entry point for headless execution
# ──────────────────────────────────────────────

if __name__ == "__main__":
    print("BlenderMCP Headless Startup")

    register_properties()

    # Use a timer to start the server after Blender is fully initialized
    def _deferred_start():
        start_server_headless()
        return None  # Don't repeat

    bpy.app.timers.register(_deferred_start, first_interval=1.0)

    print("Blender is ready for headless operation.")
