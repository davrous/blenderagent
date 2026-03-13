# Blender Scene Agent

An AI agent that creates and manipulates 3D scenes in a headless Blender instance running inside Docker. Built with the **Microsoft Agent Framework** and **Azure AI Foundry**, it communicates with Blender via the [BlenderMCP](https://github.com/ahujasid/blender-mcp) TCP socket protocol.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  Docker Container                                       │
│                                                         │
│  ┌──────────┐    ┌──────────────────┐    ┌───────────┐  │
│  │  Xvfb    │◄───│  Blender 4.2     │    │  Python   │  │
│  │ :99      │    │  (background)    │◄──►│  Agent    │  │
│  │ virtual  │    │                  │TCP │  Server   │  │
│  │ display  │    │  blender_startup │9876│  :8088    │  │
│  └──────────┘    │  .py (socket     │    │           │  │
│                  │   server)        │    │  main.py  │  │
│                  └──────────────────┘    └─────┬─────┘  │
│                                               │        │
└───────────────────────────────────────────────┼────────┘
                                                │ HTTPS
                                     ┌──────────▼──────────┐
                                     │  Azure AI Foundry   │
                                     │  (GPT-4.1-mini)     │
                                     └─────────────────────┘
```

## Features

- **Create 3D objects**: Cubes, spheres, cylinders, cones, torus, planes, monkeys
- **Apply materials**: Hex colors with metallic/roughness control
- **Poly Haven integration**: Search and download free HDRIs, textures, and 3D models
- **Viewport screenshots**: Capture and return the current viewport as base64 PNG
- **Full render**: Render scenes with EEVEE or Cycles engines
- **Arbitrary code execution**: Run custom Blender Python code for advanced operations

## Files

| File | Purpose |
|------|---------|
| `main.py` | Agent server with 13 tool functions, Azure AI Foundry client |
| `blender_startup.py` | Blender addon (runs inside Blender) - TCP socket server on port 9876 |
| `blender_connection.py` | TCP client module used by the agent to talk to Blender |
| `entrypoint.sh` | Docker entrypoint: starts Xvfb, Blender, then Agent server |
| `agent.yaml` | Agent metadata and environment variable declarations |
| `Dockerfile` | Ubuntu 22.04 + Blender 4.2 + Python deps |

## Prerequisites

- Docker
- An Azure AI Foundry project with a deployed model (e.g., `gpt-4.1-mini`)
- Azure credentials configured (e.g., `az login`)

## Build & Run

### Build the Docker image

```bash
docker build -t blender-scene-agent .

docker build --platform linux/amd64 --no-cache -t blender-scene-agent .    
```

### Run the container

```bash
docker run -it --rm \
  -p 8088:8088 \
  -e PROJECT_ENDPOINT="https://your-project.services.ai.azure.com/api/projects/your-project-id" \
  -e MODEL_DEPLOYMENT_NAME="gpt-4.1-mini" \
  -e AZURE_CLIENT_ID="..." \
  -e AZURE_TENANT_ID="..." \
  -e AZURE_CLIENT_SECRET="..." \
  blender-scene-agent
```

#### macOS / Linux

```bash
docker run -it --rm -p 8088:8088 \
  --env-file .env \
  -v ~/.azure:/root/.azure:ro \
  blender-scene-agent
```

#### Windows (PowerShell)

On Windows, the Azure CLI token cache is encrypted via DPAPI and cannot be read inside a Linux container. The entrypoint will automatically detect this and prompt you to log in via device code flow.

1. **Ensure your `.env` file uses Unix (LF) line endings**, not Windows (CRLF). CRLF line endings cause `\r` to be appended to environment variable values inside the container, which breaks authentication. You can convert it in VS Code (click "CRLF" in the status bar and select "LF") or run:
   ```powershell
   $c = Get-Content .env -Raw; $c -replace "`r`n","`n" | Set-Content .env -NoNewline
   ```

2. Run the container — no volume mount needed:
   ```powershell
   docker run -it --rm -p 8088:8088 --env-file .env blender-scene-agent
   ```
   The container will display an `az login` device code prompt. Open the URL in your browser, enter the code, and the agent will start.

   If you prefer to mount credentials explicitly (e.g., if you've set up an unencrypted token cache), use:
   ```powershell
   docker run -it --rm -p 8088:8088 --env-file .env -v "${env:USERPROFILE}/.azure:/root/.azure:ro" blender-scene-agent
   ```

Or mount Azure CLI credentials for local development (macOS/Linux):

```bash
docker run -it --rm \
  -p 8088:8088 \
  -e PROJECT_ENDPOINT="..." \
  -e MODEL_DEPLOYMENT_NAME="gpt-4.1-mini" \
  -v ~/.azure:/root/.azure:ro \
  blender-scene-agent
```

### Local development (without Docker)

1. Install dependencies: `pip install -r requirements.txt`
2. Start Blender with the socket server:
   ```bash
   blender --background --python blender_startup.py
   ```
3. In another terminal, run the agent:
   ```bash
   python main.py --port 8088
   ```

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `PROJECT_ENDPOINT` | Yes | - | Azure AI Foundry project endpoint |
| `MODEL_DEPLOYMENT_NAME` | No | `gpt-4.1-mini` | Deployed model name |
| `BLENDER_HOST` | No | `localhost` | Blender socket server host |
| `BLENDER_PORT` | No | `9876` | Blender socket server port |

## Agent Tools

| Tool | Description |
|------|-------------|
| `get_scene_info` | List all objects in the current scene |
| `get_object_info` | Get details about a specific object |
| `create_object` | Create a primitive (cube, sphere, etc.) |
| `modify_object` | Change location, rotation, or scale |
| `delete_object` | Remove an object from the scene |
| `apply_material` | Apply a colored material with metallic/roughness |
| `execute_blender_code` | Run arbitrary Python code in Blender |
| `get_viewport_screenshot` | Capture the 3D viewport as PNG |
| `search_polyhaven_assets` | Search Poly Haven for HDRIs/textures/models |
| `download_polyhaven_asset` | Download and import a Poly Haven asset |
| `apply_polyhaven_texture` | Apply a downloaded texture to an object |
| `setup_scene` | Initialize camera, lighting, and ground plane |
| `render_scene` | Render the scene with EEVEE or Cycles |

## Demos prompts

- "Load a table from Poly Haven, place it at the center, create 12 metallic cubes of various colors around it and share a high fidelity rendering of the result""
- "Add a plastic yellow sphere on top of the table"

## Credits

- [BlenderMCP](https://github.com/ahujasid/blender-mcp) by Siddharth Ahuja - TCP socket protocol
- [Poly Haven](https://polyhaven.com/) - Free HDRIs, textures, and 3D models
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) - Agent-as-Server pattern
