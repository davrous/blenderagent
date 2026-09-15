# Webchat — Custom UI for the Blender Scene Agent

A standalone web chat client for the Blender Scene Agent in [`../`](..). Streams text and inline images (viewport screenshots, renders) and surfaces `.blend` / `.glb` download links from the agent's existing tool results.

```
┌──────────────────┐    /api/chat     ┌─────────────────┐     /responses       ┌────────────────┐
│  Vite client     │◄─────SSE────────►│  Express proxy  │◄───────SSE──────────►│  Blender agent │
│  :5173           │                  │  :5174          │                      │  :8088 / Foundry│
└──────────────────┘                  └─────────────────┘                      └────────────────┘
```

The proxy exists for two reasons:
1. To inject an Entra bearer token (via `DefaultAzureCredential`) when targeting a Foundry-hosted agent — the browser cannot do this safely.
2. To work around browser CORS restrictions and SSE limitations of the Fetch API.

## Prerequisites

- Node.js 20+
- The agent reachable at one of:
  - **Local:** `agentdev run main.py --port 8088` from [`../`](..) (default for development).
  - **Foundry:** an agent deployed via `azd up` / Foundry tooling and the principal that runs the proxy has `Azure AI User` on the project plus `az login` completed.

## Quickstart — local mode

In one terminal, start the agent (from [`../`](..)):

```powershell
cd ..
agentdev run main.py --port 8088
```

In another terminal, install and start the webchat:

```powershell
npm install
npm run dev
```

Open <http://localhost:5173>. The default `.env` values target `http://localhost:8088` with no auth.

## Quickstart — Foundry mode

```powershell
copy .env.example .env
# Edit .env and set:
#   AGENT_MODE=foundry
#   AGENT_FOUNDRY_URL=https://<foundry>.services.ai.azure.com/api/projects/<project>
#   AGENT_NAME=<deployed-agent-name>
#   AGENT_API_VERSION=v1
#   MODEL_NAME=<deployed-agent-name>   # cosmetic in foundry mode

az login
npm install
npm run dev
```

> **Breaking change.** `AGENT_FOUNDRY_URL` now points at the **project endpoint** (no `/agents/<name>` suffix). The agent name is supplied separately via `AGENT_NAME` so the proxy can manage hosted-agent sessions.

If you get HTTP 401 from the proxy, try setting `AGENT_TOKEN_SCOPE=https://cognitiveservices.azure.com/.default` in `.env` — the exact required scope depends on how the deployed Foundry agent endpoint validates tokens.

## Configuration

All settings live in `webchat/.env`. See [`.env.example`](.env.example) for the full list. Highlights:

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODE` | `local` | `local` or `foundry`. |
| `AGENT_LOCAL_URL` | `http://localhost:8088` | Used when `AGENT_MODE=local`. |
| `AGENT_FOUNDRY_URL` | _(required for foundry)_ | **Project endpoint** root, e.g. `https://<acct>.services.ai.azure.com/api/projects/<project>`. |
| `AGENT_NAME` | _(required for foundry)_ | Hosted agent name, appended by the proxy. |
| `AGENT_API_VERSION` | `v1` | Foundry control-plane API version. |
| `AGENT_TOKEN_SCOPE` | `https://ai.azure.com/.default` | OAuth scope for the bearer token. |
| `MODEL_NAME` | `BlenderSceneAgent` | Sent as `model` in the Responses request body (cosmetic in foundry mode). |
| `PORT` | `5174` | Proxy listen port. |
| `VOICE_ENABLED` | `true` | Show the 🎙️ push-to-talk UI and enable the `/api/voice` relay. In local mode this must match the agent's own `ENABLE_VOICE`; in foundry mode the deployed agent must declare the `invocations_ws` protocol. |
| `VOICE_LOCAL_WS_URL` | `ws://localhost:8089/invocations_ws` | Upstream voice WebSocket for local mode. |

## Project layout

```
webchat/
├── server/                 # Express proxy: SSE pass-through + Entra auth
│   └── src/
│       ├── index.ts        # POST /api/chat, POST /api/reset, GET /api/health, GET /api/blob
│       ├── auth.ts         # Cached DefaultAzureCredential token
│       ├── sessions.ts     # Foundry hosted-agent session cache
│       ├── voice.ts        # /api/voice WebSocket relay → agent invocations_ws
│       └── config.ts
└── client/                 # Vite + React + TypeScript chat UI
    └── src/
        ├── App.tsx
        ├── api/stream.ts   # Manual SSE parser over fetch()
        ├── api/voice.ts    # Push-to-talk mic capture + PCM playback (Web Audio)
        ├── state/chatStore.ts   # zustand: messages + previous_response_id + voice
        ├── components/     # ChatView, Composer, MessageBubble, AssetGallery,
        │                   # StatusPill, ImageLightbox, DownloadButton
        ├── lib/parseMarkdown.ts
        └── styles.css
```

## How it works

- **Streaming.** The proxy forwards the agent's native SSE (`response.created`, `response.output_text.delta`, `response.completed`, …) untouched. The client parses these frames manually and appends each `delta` to the active assistant message; `react-markdown` re-renders incrementally so images appear as soon as the closing `)` of their markdown lands.
- **Multi-turn — local mode.** On `response.completed`, the client stores `response.id` and sends it as `previous_response_id` on the next request. The agent uses this to maintain `service_thread_id`, which the `SceneIsolationMiddleware` keys on for per-conversation Blender scene isolation.
- **Multi-turn — foundry mode.** The client mints a UUID `conversation_id` per session and sends it on every request. The proxy lazily creates both a Foundry hosted-agent session (sandbox/scene affinity) and a Foundry Responses conversation (persisted model transcript and Monitor grouping). Both factories are single-flight per browser UUID, so overlapping voice prewarm/reconnect and typed requests await one pending create instead of producing competing IDs. Typed requests and voice control frames carry the same two IDs, so either modality sees turns produced by the other. `Reset` deletes the session, evicts resolved and pending mappings, and rotates the browser conversation id.
- **Status pills.** The agent's `ToolStatusMiddleware` emits italic single-line markers like `*Rendering the final image…*`. The client extracts complete blocks of this shape from the streamed buffer and renders them as a pulsing badge above the message instead of inline italic text. The latest one replaces the previous; they disappear when the response completes.
- **Inline images.** Tools like `get_viewport_screenshot`, `render_preview`, and `render_final` return their results as `![label](sas-url)`. The client's custom `img` renderer wraps them in a button that opens a full-screen lightbox (click backdrop or press `Esc` to close).
- **Download buttons.** `save_scene_for_download` and `export_scene_as_glb_for_download` return `[Download…](sas-url)`. The custom `a` renderer detects `.blend` / `.glb` URLs and renders a styled download button instead of a plain link.
- **Asset galleries.** When the agent surfaces 3D models (`list_available_models`) or Poly Haven textures (`list_available_textures`), it emits the tool's JSON verbatim inside a ` ```models ` / ` ```textures ` fenced block. The client parses those blocks (`parseMarkdown.ts`), strips them from the prose, and renders clickable thumbnail galleries (`AssetGallery`). Clicking a model card asks the agent to import that GLB into the Blender scene; clicking a texture card asks it to apply that texture to an object. Galleries render the same way for typed and voice turns, and clicks are disabled while a turn is in flight.
- **Reset.** Clears local state, asks the proxy to delete the foundry session (no-op in local mode), and rotates the conversation id so the next message starts a fresh conversation (and a fresh Blender scene).
- **Voice (push-to-talk).** When `VOICE_ENABLED=true`, a 🎙️ button appears. Hold it to talk: the browser captures the mic, downsamples to 24 kHz PCM, and streams it over `/api/voice` (a WebSocket the proxy relays to the agent's `invocations_ws` server). Release to send. The agent transcribes the speech, loads the same Responses conversation as typed chat with `store:false`, streams reply `delta` frames, and speaks the prose back as 24 kHz PCM. After success, the authenticated relay appends the recognized user message and final assistant text through the Conversation Items API before forwarding `done`; this avoids the unsupported nested `/storage/responses` write from a WebSocket invocation. URLs are never read aloud. In Foundry mode the relay injects the shared `agent_session_id` + `conv_...`; locally the browser and voice pipeline advance one `previous_response_id` chain. The relay also propagates W3C trace context. Press the mic while the agent is speaking to barge in. An unexpected close clears capture/playback and the next press reconnects with the same conversation.

## Troubleshooting

- **Blank page or 502 in the browser** — the proxy is up but the agent isn't. Check `agentdev run main.py --port 8088` is running and reachable.
- **HTTP 401 in foundry mode** — token scope mismatch. Try `AGENT_TOKEN_SCOPE=https://cognitiveservices.azure.com/.default`. Also confirm `az login` succeeded as the right tenant.
- **Images don't load** — the agent uploads to Azure Blob Storage with user-delegation SAS. If the agent process can't mint SAS tokens, the URLs are unusable. See the parent README's troubleshooting section.
- **Streaming stalls** — corporate proxies often buffer SSE. The proxy sets `X-Accel-Buffering: no` but a proxy in front of `localhost` is unusual; check that nothing is intercepting `:5173` ↔ `:5174`.
- **Mic button missing** — `/api/health` must report `voiceEnabled: true` (set `VOICE_ENABLED=true`) and the browser must support `AudioContext` + `getUserMedia` + `WebSocket` (needs a secure context: `localhost` or HTTPS).
- **Voice connects but no audio / "Voice service is unavailable"** — in local mode the agent must be started with `ENABLE_VOICE=true` and `SPEECH_*` configured so its `invocations_ws` server is listening on `:8089`; in foundry mode the deployed agent must declare the `invocations_ws` protocol and its identity needs the `Cognitive Services User` role.

## Limitations / not included

- No browser-side Entra sign-in: relies on `az login` host credentials. Adequate for dev/demo; for multi-user production, layer Entra ID on top.
- The visible browser message list is not persisted across page reloads. Foundry model history is persisted in its Responses conversation while the proxy retains the browser UUID → `conv_...` mapping; production multi-replica proxies should move that mapping to shared storage.
- References and video job controls are Webchat-only; voice and Teams attachments are unchanged.
- No syntax highlighting for code blocks.

## Reference uploads and video jobs

Set `MEDIA_CONTROL_SECRET` to the **same secret of at least 32 characters** on
the Webchat server and Python agent. Do not expose it in `VITE_*` variables.
When missing or shorter, Webchat visibly disables media and keeps plain text
chat working. Set `CLIENT_ORIGIN` to the exact public browser origin (default
`http://localhost:5173`); chat, upload, approval, cancellation and reset writes
reject missing or different Origin headers. Use the Vite `/api` proxy, not
cross-origin browser requests to the server port.

Set `AZURE_STORAGE_ACCOUNT_NAME` to the agent's storage account. Uploads use
`DefaultAzureCredential` and the existing **private** `screenshots` container;
the Webchat identity needs blob read/write/delete permissions on that container.
The server refuses public containers and does not create containers or SAS tokens.
Dependencies `busboy`, `@azure/storage-blob`, `@types/busboy` and `lucide-react`
are installed through the configured approved npm feed.

Each upload streams one nonempty PNG/JPEG/WebP/MP4 file, at most 200 MiB, to a
private temporary file, checks magic bytes against the declared MIME type, and
uploads it under `references/{scope}/{uuid32}.{png|jpg|webp|mp4}`. Multipart fields
and extra files are rejected. At most four owned blob names can accompany a turn;
URLs and references from another browser/conversation are rejected. Temporary
files are removed on success, rejection and disconnection. Failed Blob writes
attempt deletion; if Azure is unavailable, uncommitted blocks or orphaned blobs
can remain. Removing a successfully uploaded attachment only removes it from
the draft; configure storage lifecycle retention for unused references.

Install `ffprobe` on the **Webchat host**, not only in the agent container, for
server metadata checks: images <=16,000,000 pixels; video <=1920x1080 and 4-30
seconds. Probe failures reject the file. If the executable is absent, only file
size, declared type and magic bytes are checked on Webchat; the upload reports
`metadata_validated:false` and the composer displays validation pending. Magic
bytes and ffprobe metadata are not full decode validation. **The Python backend
must decode/ffprobe and reject invalid media before analysis** in either case.
Uploads time out after three minutes; individual probes after twenty seconds.

### Backend contract and security

- Every media-enabled chat input is a string
  `BLENDER_MEDIA_V1:<base64url UTF8 JSON>.<lowercase hex HMAC-SHA256>`; the MAC
  covers the base64url part. JSON is `{scope,text,references?,action?}`.
- Scope is `sha256(browserKey + ':' + lowercaseConversationUUID).slice(0,32)`.
  The opaque random browser key is in a one-year HttpOnly SameSite=Strict cookie,
  signed using the shared secret with a separate `browser:` domain prefix.
  HTTPS origins use Secure cookies. Scope is never accepted from the browser.
  Cookies/scopes survive server restarts with the same secret; clearing cookies
  or rotating the secret loses access to old references/jobs. This is browser
  ownership isolation, not a replacement for production user authentication.
- Browser text resembling an envelope is nested safely as `text` in a new signed
  envelope, never forwarded as a control. With media disabled such prefixes are
  rejected. The agent must unwrap once and must never interpret the unwrapped
  text or model-generated instructions as authorized control actions.
- `GET /api/video-jobs/:id?conversation_id=...` sends a signed status action;
  POST `.../approve` sends `{conversation_id,prompt,resolution,generate_audio}`
  after an explicit user click; POST `.../cancel` takes `{conversation_id}`.
  Before a mutation, the server obtains live status under the same job lock.
  Approval requires `awaiting_seedance_approval` and `seedance_enabled:true`.
  Python must independently enforce scope ownership, current state, atomic
  approval/idempotency and cancellation. No manifest or arbitrary URL is read
  by these control routes, and approval requests are never automatically retried.
- Controls use the existing local/Foundry Responses builder with `stream:true`,
  `store:false`, the same `agent_session_id`, and **no** `conversation` or
  `previous_response_id`. Foundry `user`/`metadata.conversation_id` are retained
  for scene affinity, without creating a Responses conversation for controls.
  The backend must return the requested descriptor in output_text.delta SSE
  chunks as a closed `videojob` JSON fence, even when no model is involved.
- Descriptor fields are `id,state,progress,mode,duration_seconds,fps,resolution,
  seedance_enabled` plus optional `preview_url,poster_url,output_url,error,
  estimate_usd`. IDs must be 32 lowercase hex. Known states are `queued`,
  `rendering`, `encoding`, `awaiting_seedance_approval`, `wavespeed_uploading`,
  `wavespeed_submitting`, `wavespeed_processing`, `submission_unknown`,
  `completed`, `failed`, `cancelled`. Progress is a percentage from 0 to 100;
  other numeric fields must be finite and nonnegative. Media URLs
  must be HTTPS on configured Blob hosts, and are played through `/api/blob`
  with Range support. The proxy rejects redirects. The backend must refresh
  expired media URLs in live status replies.
- Job IDs, not descriptors or control messages, are saved per conversation in
  localStorage (most recent 100). Reload fetches live state; transcript and
  previous-response state are not modified by polling. Polls are single-flight,
  7.5 seconds after the preceding response, paused during typed/voice work, with
  a 45-second server deadline. Errors stop polling and offer Retry status.
  Completed/failed/cancelled/**submission_unknown** stop automatic polling;
  unknown submission must be reconciled externally, never auto-resubmitted.
- Paid processing defaults to 720p and no audio. Explicit consent covers external
  WaveSpeed/Seedance processing and the estimate. Rates per **input + output**
  second: 480p $0.11, 720p $0.22, 1080p $0.55, 4k $1.10. With no separate output
  duration in the contract, the displayed estimate assumes output duration equals
  `duration_seconds` of the input preview. Actual billing may differ.

### Offline checks

From the repository root:

```powershell
npm --prefix webchat run build
node devTools/test_video_webchat.mjs
node devTools/test_voice_relay.mjs
```

The media suite uses fake local Responses and Blob operations. It never invokes
the real agent, uploads to Azure, deploys resources or submits paid video jobs.
