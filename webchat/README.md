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
        ├── components/     # ChatView, Composer, MessageBubble,
        │                   # StatusPill, ImageLightbox, DownloadButton
        ├── lib/parseMarkdown.ts
        └── styles.css
```

## How it works

- **Streaming.** The proxy forwards the agent's native SSE (`response.created`, `response.output_text.delta`, `response.completed`, …) untouched. The client parses these frames manually and appends each `delta` to the active assistant message; `react-markdown` re-renders incrementally so images appear as soon as the closing `)` of their markdown lands.
- **Multi-turn — local mode.** On `response.completed`, the client stores `response.id` and sends it as `previous_response_id` on the next request. The agent uses this to maintain `service_thread_id`, which the `SceneIsolationMiddleware` keys on for per-conversation Blender scene isolation.
- **Multi-turn — foundry mode.** The client mints a UUID `conversation_id` per session and sends it on every request. The proxy lazily creates a Foundry hosted-agent session (`POST /agents/{name}/endpoint/sessions`) keyed off that id and reuses the resulting `agent_session_id` on every `/responses` call. `Reset` (and the `/api/reset` endpoint) deletes the session server-side and rotates the conversation id, which gives the next message a fresh Blender scene.
- **Status pills.** The agent's `ToolStatusMiddleware` emits italic single-line markers like `*Rendering the final image…*`. The client extracts complete blocks of this shape from the streamed buffer and renders them as a pulsing badge above the message instead of inline italic text. The latest one replaces the previous; they disappear when the response completes.
- **Inline images.** Tools like `get_viewport_screenshot`, `render_preview`, and `render_final` return their results as `![label](sas-url)`. The client's custom `img` renderer wraps them in a button that opens a full-screen lightbox (click backdrop or press `Esc` to close).
- **Download buttons.** `save_scene_for_download` and `export_scene_as_glb_for_download` return `[Download…](sas-url)`. The custom `a` renderer detects `.blend` / `.glb` URLs and renders a styled download button instead of a plain link.
- **Reset.** Clears local state, asks the proxy to delete the foundry session (no-op in local mode), and rotates the conversation id so the next message starts a fresh conversation (and a fresh Blender scene).
- **Voice (push-to-talk).** When `VOICE_ENABLED=true`, a 🎙️ button appears. Hold it to talk: the browser captures the mic, downsamples to 24 kHz PCM, and streams it over `/api/voice` (a WebSocket the proxy relays to the agent's `invocations_ws` server). Release to send. The agent transcribes the speech, runs the *same* turn as the typed path (so voice and text share one Blender scene keyed by `conversation_id`), streams the reply text back as `delta` frames (rendered identically, with status pills, screenshots, and download buttons), and speaks the prose back as 24 kHz PCM audio. URLs are never read aloud — a short spoken cue announces screenshots/renders/downloads. Continuity is client-owned: the browser sends `conversation_id` + `previous_response_id` in the `commit` frame and updates `previous_response_id` from the `done` frame. Press the mic while the agent is speaking to barge in.

## Troubleshooting

- **Blank page or 502 in the browser** — the proxy is up but the agent isn't. Check `agentdev run main.py --port 8088` is running and reachable.
- **HTTP 401 in foundry mode** — token scope mismatch. Try `AGENT_TOKEN_SCOPE=https://cognitiveservices.azure.com/.default`. Also confirm `az login` succeeded as the right tenant.
- **Images don't load** — the agent uploads to Azure Blob Storage with user-delegation SAS. If the agent process can't mint SAS tokens, the URLs are unusable. See the parent README's troubleshooting section.
- **Streaming stalls** — corporate proxies often buffer SSE. The proxy sets `X-Accel-Buffering: no` but a proxy in front of `localhost` is unusual; check that nothing is intercepting `:5173` ↔ `:5174`.
- **Mic button missing** — `/api/health` must report `voiceEnabled: true` (set `VOICE_ENABLED=true`) and the browser must support `AudioContext` + `getUserMedia` + `WebSocket` (needs a secure context: `localhost` or HTTPS).
- **Voice connects but no audio / "Voice service is unavailable"** — in local mode the agent must be started with `ENABLE_VOICE=true` and `SPEECH_*` configured so its `invocations_ws` server is listening on `:8089`; in foundry mode the deployed agent must declare the `invocations_ws` protocol and its identity needs the `Cognitive Services User` role.

## Limitations / not included

- No browser-side Entra sign-in: relies on `az login` host credentials. Adequate for dev/demo; for multi-user production, layer Entra ID on top.
- No persistent history across browser sessions.
- No file upload to the agent.
- No syntax highlighting for code blocks.
