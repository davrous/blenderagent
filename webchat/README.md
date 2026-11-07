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
| `MUSIC_ENABLED` | `false` | Offer the separate film-music mode; requires enabled voice/media controls and an audio model configured on the Python agent. |

## Devfest music mode

Set `MUSIC_ENABLED=true` here and configure `MUSIC_AUDIO_ENDPOINT` and
`MUSIC_MODEL_DEPLOYMENT_NAME` on the agent. Rebuild/restart both sides after
the changes. An existing deployed agent must also receive the updated Python
code and configuration; deploying the audio model alone does not update it.
The agent identity needs audio-resource inference permission, even if the
developer's CLI smoke test succeeds. See [the agent setup](../README.md#devfest-film-music-mode).

**Dialogue vocal** stays the default push-to-talk mode. **Musique de film**
uses click-to-start/click-to-stop, recording 2-30 seconds without speech noise
suppression. Musical notes animate during analysis, followed by a proposed
film/title and uncertainty. No Blender tools run during recognition.

Use the confirmation button or type `oui` to create the original developer
vignette with original film-inspired caricatures (choose 5, 10 or 15 seconds,
Devfest Toulouse
greeting). The musical-mode selectors choose **Clay** (neutral gray, fast)
or **Complet** (materials and lighting); defaults are 15 seconds and full rendering.
The options are signed with the confirmation request and enforced on the camera,
timeline and saved snapshot, not applied as a later truncation.
The existing viewport shows the scene, and the video card first offers
a packed `.blend` download. A frame-1 viewport screenshot appears above the card
and opens in the existing lightbox. It is captured during preparation, not left
to a discretionary model tool call. Open the `.blend` in Blender and play the timeline, then check
the inspection confirmation to launch the **480p / 25 fps** render. No render
starts automatically during preparation or status polling, apart from the single
fast viewport still. Clicking the render button immediately changes its label
and shows a spinner/status message while the server validates the request.
Only after acknowledgement does the UI show accepted/queued, rendering or
encoding. Duplicate clicks cannot send a second action.

Once that video completes, approve the card's **1080p / 25 fps** option if the
preview pleases you. It rerenders the same frozen animation and selected Clay/full
style, preserving the 480p
download; it does not regenerate the scene or send it to Seedance. HD calculates
about five times more pixels and remains subject to the 60-minute render timeout.
The `.blend` includes packed resources and stays available on the same card.
Refusal and
discussion do not generate a scene. Switch back to **Dialogue vocal** or Reset
to discard the pending music workflow. Approval never authorizes paid Seedance.
Musical input audio is kept as a private WAV for replay, but is not used as the
rendered video's soundtrack.

## Private realizations gallery

Configure `AZURE_STORAGE_ACCOUNT_NAME` on this proxy as well as the agent and
keep the signed browser cookie (`MEDIA_CONTROL_SECRET` must remain stable).
Startup offers **Réalisations** and **Nouvelle scène**. Clicking a thumbnail
loads the archived text/voice history, native job IDs and the distinct persisted
Foundry sandbox/conversation associations. New work no longer deletes previous
sandboxes. While a turn is active, navigation is disabled.

Each thumbnail has a **delete** control with an irreversible-deletion
confirmation. Only the verified owning browser can rename or delete, and the
server requires `confirm: true` for deletion. Rendering, running operations,
leased artifacts and ambiguous paid submissions block deletion. Cleanup uses
persisted Foundry sandbox/conversation IDs rather than process caches, deletes
conversation items before their conversation, and removes only exact,
version-checked files in the workspace's verified media scope. Shared referenced
files and legacy unscoped artifacts whose ownership cannot be proven are kept;
no shared directories or containers are removed.

A durable deletion record replaces the archived transcript before cleanup.
`workspace-scenes/{scope}/deleted.json` stays permanently to prevent legacy
imports and late agent callbacks from recreating deleted work. If cleanup fails,
the gallery shows a non-resumable pending-deletion tile; use its trash control
again to retry the remaining cleanup. Do not remove tombstones manually.
Local cleanup is a signed `workspace_delete` control sent with `stream:false`,
acknowledged with the exact `{"workspace_deleted":"<UUID>"}` JSON; it bypasses
the LLM and targets only that conversation's local checkpoint.

The existing Blender LLM reply supplies a short French title in a private
`workspacetitle` JSON fence. The UI hides that metadata, and the archive accepts
only the first valid generated title. No separate paid title-generation call
is made. Use the pencil control to edit the title (1–90 characters); manual
titles remain protected against late replies and concurrent archive retries.

**Discussion** continues the selected work; **Ressources** browses all recorded
images, videos, musical inputs and downloads in chronological order. Native
video cards still provide their existing render/approval controls. Private
resource URLs are renewed on access. Local resumed voice/typed turns replay
archived prose when their in-process response chain is no longer available.

Metadata lives in `webchat-workspaces/` in the existing private `screenshots`
container. Gallery ownership is tied to the signed HttpOnly cookie and endpoint,
not caller-supplied Foundry IDs. Scene checkpoints live under
`workspace-scenes/{scope}/scene.blend`; their packed contents can restore work
after a local container's files disappear. The proxy requires Blob write/read
and delegation rights. Keep storage private; do not commit archive credentials.

Known legacy browser IDs/jobs are imported where possible. This cannot recover
previously deleted compute files or microphone data that was never saved.
If gallery configuration/storage is unavailable, the error is explicit; the
existing text-only client behavior remains available when gallery is disabled.

Offline workspace checks: `node devTools/test_workspaces.mjs` and
`node devTools/test_workspace_management.mjs` from the repository root. Tests
use memory storage and fake Foundry endpoints; they never delete real records
or invoke a paid model.

After `npm run build`, run `node ../devTools/test_music_webchat.mjs` from this
directory (or `node devTools/test_music_webchat.mjs` from the repository root).

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
- **Multi-turn — local mode.** On `response.completed`, the client stores `response.id` and sends it as `previous_response_id` on the next request for response-history continuity. Blender still has one shared scene per container; the stable browser conversation identity supports reset detection, not separate per-user scene files.
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
- Reference uploads and paid approval use visual controls. Voice can request video work in the same signed media scope but cannot approve payment. Teams also supports references and video cards, including in-place progress; see the [main video workflow](../README.md#reference-to-video-workflow).
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
  `rendering`, `encoding`, `awaiting_render_approval`, `awaiting_seedance_approval`, `wavespeed_uploading`,
  `wavespeed_submitting`, `wavespeed_processing`, `submission_unknown`,
  `completed`, `failed`, `cancelled`. Progress is a percentage from 0 to 100;
  other numeric fields must be finite and nonnegative. Media URLs
  must be HTTPS on configured Blob hosts, and are played through `/api/blob`
  with Range support. The proxy rejects redirects. The backend must refresh
  expired media URLs in live status replies.
- Devfest inspection descriptors additionally contain `inspection_required:true`,
  a validated Blob `scene_url` and `hd_available`, plus `viewport_url` for new
  preparations. The viewport URL is validated and refreshed like other media links.
  Previously inspected jobs without this field remain valid. The native `POST
  /api/video-jobs/:id/render` action accepts only `resolution:"480p"` or
  `"1080p"` (and `conversation_id`), without paid prompt/audio options.
  It requires verified browser ownership and Origin: 480p is eligible only while
  awaiting render approval; 1080p only after a completed 480p preview with HD
  available. Both actions operate on the same job and frozen `.blend`.
- Job IDs, not descriptors or control messages, are saved per conversation in
  localStorage (most recent 100). Reload fetches live state; transcript and
  previous-response state are not modified by polling. Polls are single-flight,
  7.5 seconds after the preceding response, paused during typed/voice work, with
  a 45-second server deadline. Errors stop polling and offer Retry status.
  Completed/failed/cancelled/**submission_unknown** and `awaiting_render_approval` stop automatic polling;
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
