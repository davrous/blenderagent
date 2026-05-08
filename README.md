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
- **Per-conversation scene isolation**: Each conversation gets its own Blender scene, saved/restored from Azure Blob Storage

## Files

| File | Purpose |
|------|---------|
| `main.py` | Agent server with 13 tool functions, Azure AI Foundry client |
| `blender_startup.py` | Blender addon (runs inside Blender) - TCP socket server on port 9876 |
| `blender_connection.py` | TCP client module used by the agent to talk to Blender |
| `scene_manager.py` | Per-conversation Blender scene isolation with Azure Blob Storage persistence |
| `entrypoint.sh` | Docker entrypoint: starts Xvfb, Blender, then Agent server |
| `agent.yaml` | Agent metadata and environment variable declarations |
| `Dockerfile` | Ubuntu 22.04 + Blender 4.2 + Python deps |

## Per-Conversation Scene Isolation

The agent supports **multiple concurrent conversations**, each with its own isolated Blender scene. This is handled by the `SceneIsolationMiddleware` (in `main.py`) and `SceneManager` (in `scene_manager.py`).

### How it works

1. **User A** starts a conversation and builds a scene. At the end of each request, the Blender scene is saved as a `.blend` file and uploaded to Azure Blob Storage, keyed by the conversation's thread ID.
2. **User B** starts a separate conversation. User A's scene is automatically saved, Blender is reset to a clean state, and User B gets a fresh scene.
3. **User A returns** in the same conversation. User B's scene is saved, and User A's scene is restored from Blob Storage — exactly as they left it.

Scenes are stored in the `blender-scenes` container in Azure Blob Storage under `scenes/<thread-id>.blend`. **Both the `screenshots` and `blender-scenes` containers must be pre-created** — the agent no longer creates them at runtime (least-privilege RBAC on the Foundry agent identity does not include management-plane permissions).

### Thread ID lifecycle

The conversation identifier comes from `context.thread.service_thread_id` in the Microsoft Agent Framework. On the **first request** of a new conversation, this ID is `None` (the Azure AI service assigns it during the streaming run). The middleware handles this by:
- Skipping scene activation on first request (Blender starts clean)
- Reading the thread ID **after streaming completes** (by which point the framework has set it) for the save operation
- On subsequent requests, the thread is loaded from the `InMemoryAgentThreadRepository` with the ID already set

## Middleware

The agent is wired with two custom middlewares that sit on top of the Microsoft Agent Framework:

```python
middleware=[SceneIsolationMiddleware(ToolStatusMiddleware(), scene_manager)],
```

The outer one runs first on the way in and last on the way out; the inner one sits closest to the LLM and tool execution. Together they add capabilities the framework does not provide on its own.

### `SceneIsolationMiddleware` (outer)

Provides **per-conversation Blender scene persistence** on top of a single shared Blender process:

- **Conversation ID resolution** — tries `context.session.session_id`, then `context.options` / `context.kwargs` / `context.metadata` (`user`, `metadata.conversation_id`), then `agent._request_headers` (the only channel that propagates the conversation ID through Foundry's OpenAI Responses path). Includes a one-shot diagnostic dump to debug routing differences between local agentdev and Foundry.
- **Activate before the run** — loads the conversation's previously-saved `.blend` from blob storage into Blender. If the ID isn't resolvable yet (first turn of a new conversation), it saves any other conversation's scene currently loaded and resets Blender to a clean state, so conversations cannot leak geometry into each other.
- **Save after streaming completes** — wraps `context.result` in a generator with a `finally` block so the scene is uploaded to blob storage *after* the last chunk is yielded. This timing matters: the framework only mutates the thread with its real `service_thread_id` after the stream ends, so saving any earlier would miss the ID on the very first turn.

### `ToolStatusMiddleware` (inner)

Transforms the raw streaming response into a richer UX stream for the WebChat client:

- **Human-readable status pills** — when a `FunctionCallContent` chunk is seen for a tool such as `render_final` or `download_polyhaven_asset`, an extra status message ("Rendering the final image…", "Downloading asset from Poly Haven…") is emitted via the `_TOOL_STATUS_MESSAGES` map. Without this the user just sees a long pause while a tool runs.
- **Deduplication** — the framework can emit multiple `FunctionCallContent` chunks with different `call_id`s for one logical invocation; a per-turn `announced_names` set ensures only one pill per tool.
- **Early image surfacing** — for image-producing tools (`get_viewport_screenshot`, `render_preview`, `render_final`) the markdown image is pulled out of the tool result and streamed immediately, instead of waiting for the model to echo it in its final answer.
- **Early download-link surfacing** — same treatment for `save_scene_for_download` and `export_scene_as_glb_for_download`.
- **Heartbeats and per-turn timeout** — a background pump task feeds chunks into a queue so heartbeat messages can be interleaved without cancelling an in-flight HTTP read; if the turn exceeds `TURN_TIMEOUT_SECONDS`, a friendly timeout message is yielded instead of a raw exception.
- **Friendly error mapping** — on upstream stream failures, structured diagnostics (session id, elapsed ms, status code, request id, exception type) are logged and a friendly model-error message is yielded to the user before the exception is re-raised for telemetry.

### Why this composition order

`SceneIsolation` *outside* `ToolStatus` is deliberate: scene activation must happen *before* any tool runs, and scene save must happen *after* the entire streamed response (including status messages and surfaced images) has been delivered to the client.

| Concern | Provided by |
|---|---|
| Per-conversation isolation of an external stateful process (Blender) | `SceneIsolationMiddleware` |
| Loading/saving that state to blob storage at the right point in the request lifecycle | `SceneIsolationMiddleware` |
| Resolving conversation IDs across local agentdev vs. Foundry routing quirks | `SceneIsolationMiddleware` |
| Tool-call → user-facing status messages | `ToolStatusMiddleware` |
| Streaming images / download links the moment the tool returns them | `ToolStatusMiddleware` |
| Heartbeats and per-turn timeout with friendly fallback text | `ToolStatusMiddleware` |
| Structured diagnostics on stream failure | `ToolStatusMiddleware` |

## Prerequisites

- Docker
- An Azure AI Foundry project with a deployed model (e.g., `gpt-4.1-mini`)
- Azure credentials configured (e.g., `az login`)
- The principal that runs the agent must have the **Storage Blob Data Contributor** *and* **Storage Blob Delegator** roles on the storage account. The first lets it upload/download blobs; the second is required by `get_user_delegation_key` to mint the SAS URLs returned to the user. The principal is:
  - **Local development:** your own Azure account (the one used with `az login`).
  - **Hosted in Azure AI Foundry (ADC, current platform):** a per-agent service identity automatically provisioned by Foundry, named `<foundry>-<project>-<agent>-AgentIdentity` (type `ServiceIdentity`). **This is not the Foundry project's managed identity** — it is a separate principal created for each agent. See [step 2](#2-assign-the-storage-blob-data-contributor-role) below for how to find its object ID and grant the roles.

## Setup for your own Azure environment

### 1. Create an Azure Blob Storage account and container

The agent uploads viewport screenshots and rendered images to Azure Blob Storage so they can be returned as URLs to the user (see the `upload_image_to_blob` function in `main.py`). The container used is called **`screenshots`**.

Create a storage account (or use an existing one):

```bash
az storage account create \
  --name <your-storage-account-name> \
  --resource-group <your-resource-group> \
  --location <your-location> \
  --sku Standard_LRS \
  --allow-blob-public-access true
```

Then **pre-create both containers** (the agent does not create them at runtime):

```bash
az storage container create --account-name <your-storage-account-name> --auth-mode login --name screenshots
az storage container create --account-name <your-storage-account-name> --auth-mode login --name blender-scenes
```

### 2. Assign the Storage Blob Data Contributor role

The agent authenticates to Blob Storage using `DefaultAzureCredential`. Two roles are required on the storage account:

- **`Storage Blob Data Contributor`** — read/write/delete blobs (screenshots, `.blend` scene files).
- **`Storage Blob Delegator`** — mint user-delegation SAS tokens (the agent returns SAS URLs for screenshots; without this role the upload succeeds but the URL is unusable).

#### 2a. Hosted in Azure AI Foundry (ADC platform)

Since the migration from ACA to ADC, Foundry runs each agent under its own auto-provisioned **service identity** — *not* the Foundry project's managed identity. The identity is named `<foundry>-<project>-<agent>-AgentIdentity` and only has `Azure AI User` on the project by default. **Any RBAC you previously granted to your own user-assigned MI on ACA does not carry over and must be re-granted to this new principal.**

To discover the object ID of the agent identity, the easiest way is to deploy the agent once and let it log the principal at first use — [main.py](main.py) calls `_log_storage_principal_once()` on the first blob upload, which prints a line like:

```
INFO: blender_agent: Storage MSI principal: oid=<GUID> appid=<GUID> tid=<GUID> ...
```

Alternatively, list it directly:

```bash
az ad sp list --display-name "<foundry-resource>-<project>-<agent>-AgentIdentity" --query "[].{displayName:displayName,id:id,appId:appId}" -o table
```

Then grant both roles:

```bash
OID=<object-id-of-the-AgentIdentity>
SCOPE=/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Storage/storageAccounts/<your-storage-account-name>

az role assignment create \
  --assignee-object-id $OID \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Data Contributor" \
  --scope $SCOPE

az role assignment create \
  --assignee-object-id $OID \
  --assignee-principal-type ServicePrincipal \
  --role "Storage Blob Delegator" \
  --scope $SCOPE
```

Verify:

```bash
az role assignment list --assignee $OID --all -o table
```

#### 2b. Local development

Assign the same two roles to your own Azure account:

```bash
az role assignment create \
  --assignee <your-azure-account-email-or-object-id> \
  --role "Storage Blob Data Contributor" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Storage/storageAccounts/<your-storage-account-name>

az role assignment create \
  --assignee <your-azure-account-email-or-object-id> \
  --role "Storage Blob Delegator" \
  --scope /subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.Storage/storageAccounts/<your-storage-account-name>
```

#### Troubleshooting `AuthorizationPermissionMismatch`

If blob calls return HTTP 403 `AuthorizationPermissionMismatch` (different from `AuthorizationFailure`), it means the principal **has a token but the wrong role**. Check the logs for the `Storage MSI principal: oid=...` line and confirm that exact OID has both roles on the storage account scope. RBAC propagation can take 1–2 minutes.

### 3. Update the `.env` file

Copy or edit the `.env` file at the root of this project to match your environment:

```env
PROJECT_ENDPOINT=https://<your-foundry-resource>.services.ai.azure.com/api/projects/<your-project-name>
MODEL_DEPLOYMENT_NAME=gpt-4.1-mini
AZURE_STORAGE_ACCOUNT_NAME=<your-storage-account-name>
```

| Variable | Description |
|----------|-------------|
| `PROJECT_ENDPOINT` | The full endpoint URL of your Azure AI Foundry project |
| `MODEL_DEPLOYMENT_NAME` | The name of the model deployment to use (e.g., `gpt-4.1-mini`) |
| `AZURE_STORAGE_ACCOUNT_NAME` | The name of the Azure Storage account created in step 1 |

## Deploying to Azure AI Foundry

> ⚠️ **Set `AI_FOUNDRY_ACR_BUILD_WAIT_UNTIL_DONE=true` before deploying.**
>
> This agent's Docker image installs Blender (and its system dependencies), which makes the Azure Container Registry build noticeably long. Without this flag, the Foundry deploy command may return a timeout while the ACR build is still running, leaving you unsure whether the deployment succeeded. Setting it forces the deploy tooling to wait until the ACR build actually finishes.
>
> Set it in the **shell from which you launch the deployment** (not inside the container):
>
> **PowerShell**
> ```powershell
> $env:AI_FOUNDRY_ACR_BUILD_WAIT_UNTIL_DONE = "true"
> ```
>
> **bash / zsh**
> ```bash
> export AI_FOUNDRY_ACR_BUILD_WAIT_UNTIL_DONE=true
> ```

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

```powershell
docker run -it --rm -p 8088:8088 --env-file .env -v ~/.azure:/root/.azure:ro blender-scene-agent
```

If the container can't read your Azure credentials (you'll see a `DefaultAzureCredential` error at startup), it's because the Azure CLI on Windows encrypts the token cache with DPAPI by default and a Linux container can't decrypt it. Run this once on your host to switch to a plaintext cache (same behaviour as macOS/Linux), then retry:

```powershell
az config set core.encrypt_token_cache=false
az account clear
az login
```

> **Security note:** tokens are then stored in plaintext in `%USERPROFILE%\.azure`. Re-enable later with `az config set core.encrypt_token_cache=true` if needed.

**Fallback** (no host changes): omit the `-v` mount and the container will fall back to `az login --use-device-code`:

```powershell
docker run -it --rm -p 8088:8088 --env-file .env blender-scene-agent
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
| `save_scene_for_download` | Save the scene as a .blend file and return a download link (expires after 1 hour) |

## Demos prompts

- "Load a table from Poly Haven, place it at the center, create 12 metallic cubes of various colors around it and share a high fidelity rendering of the result"
- "Add a plastic yellow sphere on top of the table"

![Screenshot of the Foundry Hosted Blender Agent in action](ScreenshotDemoFoundryBlenderAgent.jpg)

- Create a fantasy world for kids, use basic primitives to build 6 houses and 10 trees. A kid should later be able to navigate in this 3D world on a path connecting each house. Give me a high fidelity rendering at the end
- give me the GLB version

Then, drag'n'drop the GLB file in https://sandbox.babylonjs.com for instance

![Screenshot of a glTF export of the Blender Hosted Agent running in Babylon.js](ScreenshotFoundryHAGLB.png)

## Credits

- [BlenderMCP](https://github.com/ahujasid/blender-mcp) by Siddharth Ahuja - TCP socket protocol
- [Poly Haven](https://polyhaven.com/) - Free HDRIs, textures, and 3D models
- [Microsoft Agent Framework](https://github.com/microsoft/agent-framework) - Agent-as-Server pattern
