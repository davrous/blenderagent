import express from "express";
import cors from "cors";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { config } from "./config.js";
import { getBearerToken } from "./auth.js";
import {
  getOrCreateSession,
  evictSession,
  deleteSession,
} from "./sessions.js";
import { attachVoiceRelay } from "./voice.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(
  cors({
    origin: config.clientOrigin,
  }),
);

/**
 * Static assets shipped with the server (e.g. the HDR environment map used by
 * the Babylon.js viewer to match Blender's PolyHaven world lighting).
 *
 * Mounted under `/api/assets` so the existing Vite dev proxy (`/api` →
 * server) handles it transparently without extra config. Both `tsx src/index.ts`
 * and the compiled `dist/index.js` resolve `../assets` to `webchat/server/assets`.
 */
app.use(
  "/api/assets",
  express.static(path.join(__dirname, "../assets"), {
    maxAge: "1h",
    fallthrough: false,
  }),
);

app.get("/api/health", (_req, res) => {
  res.json({
    mode: config.mode,
    agentUrl: config.agentUrl,
    agentName: config.mode === "foundry" ? config.agentName : undefined,
    apiVersion: config.mode === "foundry" ? config.apiVersion : undefined,
    model: config.modelName,
    voiceEnabled: config.voiceEnabled,
  });
});

/**
 * Streaming proxy for blob URLs that don't expose CORS headers (e.g. Azure
 * Blob Storage SAS URLs). Used by the Babylon.js 3D viewer which loads GLBs
 * via XHR and therefore needs same-origin / CORS-compliant responses.
 *
 * Hosts are validated against `config.blobProxyAllowedHostSuffixes` to avoid
 * turning the server into an open proxy / SSRF vector.
 */
app.get("/api/blob", async (req, res) => {
  const rawUrl = typeof req.query.url === "string" ? req.query.url : "";
  if (!rawUrl) {
    res.status(400).json({ error: "url query param is required" });
    return;
  }

  let target: URL;
  try {
    target = new URL(rawUrl);
  } catch {
    res.status(400).json({ error: "invalid url" });
    return;
  }

  if (target.protocol !== "https:" && target.protocol !== "http:") {
    res.status(400).json({ error: "only http(s) urls allowed" });
    return;
  }

  const host = target.hostname.toLowerCase();
  const allowed = config.blobProxyAllowedHostSuffixes.some(
    (suffix) => host === suffix || host.endsWith(suffix),
  );
  if (!allowed) {
    res.status(403).json({ error: `host not allowed: ${host}` });
    return;
  }

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: "GET",
      headers: {
        // Forward Range for partial requests if the client uses them.
        ...(req.headers.range ? { Range: String(req.headers.range) } : {}),
      },
    });
  } catch (err) {
    console.error("Blob proxy fetch failed:", err);
    res.status(502).json({
      error: "Failed to fetch blob",
      detail: err instanceof Error ? err.message : String(err),
    });
    return;
  }

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "");
    res.status(upstream.status || 502).json({
      error: `Upstream returned ${upstream.status}`,
      detail: text.slice(0, 500),
    });
    return;
  }

  res.status(upstream.status);
  const passthroughHeaders = [
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
    "etag",
    "last-modified",
  ];
  for (const name of passthroughHeaders) {
    const v = upstream.headers.get(name);
    if (v) res.setHeader(name, v);
  }
  // Allow long-term caching on the client.
  res.setHeader("Cache-Control", "private, max-age=300");

  const reader = upstream.body.getReader();
  req.on("close", () => {
    reader.cancel().catch(() => {});
  });

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) res.write(Buffer.from(value));
    }
  } catch (err) {
    console.error("Blob proxy stream error:", err);
  } finally {
    if (!res.writableEnded) res.end();
  }
});

interface ChatRequestBody {
  input?: string;
  previous_response_id?: string | null;
  conversation_id?: string;
}

function isUuid(v: unknown): v is string {
  return (
    typeof v === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(
      v,
    )
  );
}

async function buildUpstreamRequest(
  body: ChatRequestBody,
  input: string,
): Promise<{ url: string; headers: Record<string, string>; payload: Record<string, unknown>; conversationId?: string }> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const token = await getBearerToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  // The conversation id is the stable scene key on the agent side.
  // It is REQUIRED in both modes so the agent can save/reload the per-
  // conversation Blender scene blob across container recycling and tab
  // refreshes.
  const conversationId = body.conversation_id;
  if (!isUuid(conversationId)) {
    throw new Error("conversation_id (UUID) is required");
  }

  if (config.mode === "local") {
    const payload: Record<string, unknown> = {
      model: config.modelName,
      input,
      stream: true,
      // Bind every turn to the same agentdev session so
      // `context.session.session_id` is populated for SceneIsolationMiddleware.
      // Without this, the middleware logs `session=None` and the scene is
      // never saved to blob storage, so it cannot survive container recycle.
      agent_session_id: conversationId,
    };
    if (body.previous_response_id) {
      payload.previous_response_id = body.previous_response_id;
    }
    console.log(
      `[chat] local mode: conversation=${conversationId} previous_response_id=${body.previous_response_id ?? "none"}`,
    );
    return { url: `${config.agentUrl}/responses`, headers, payload, conversationId };
  }

  // Foundry mode
  const { agentSessionId } = await getOrCreateSession(conversationId);

  headers["Foundry-Features"] = config.foundryFeaturesHeader;

  const url = `${config.foundryAgentBase}/endpoint/protocols/openai/responses?api-version=${encodeURIComponent(
    config.apiVersion,
  )}`;

  const payload: Record<string, unknown> = {
    model: config.modelName,
    input,
    stream: true,
    agent_session_id: agentSessionId,
    // The Foundry `agent_session_id` is a Foundry-internal session id that
    // does NOT propagate to `context.session.session_id` in the agent_framework
    // middleware. We therefore also pass the WebChat conversation UUID through
    // the standard OpenAI `user` and `metadata` fields so SceneIsolationMiddleware
    // can recover a stable scene key.
    user: conversationId,
    metadata: { conversation_id: conversationId },
  };

  console.log(
    `[chat] foundry mode: conversation=${conversationId} agent_session_id=${agentSessionId}`,
  );

  return { url, headers, payload, conversationId };
}

app.post("/api/chat", async (req, res) => {
  const body = req.body as ChatRequestBody;
  const input = (body.input ?? "").toString();
  if (!input.trim()) {
    res.status(400).json({ error: "input is required" });
    return;
  }

  let upstreamReq: Awaited<ReturnType<typeof buildUpstreamRequest>>;
  try {
    upstreamReq = await buildUpstreamRequest(body, input);
  } catch (err) {
    console.error("Failed to build upstream request:", err);
    res.status(500).json({
      error: "Failed to prepare upstream request",
      detail: err instanceof Error ? err.message : String(err),
    });
    return;
  }

  const doFetch = async (): Promise<Response> => {
    return fetch(upstreamReq.url, {
      method: "POST",
      headers: upstreamReq.headers,
      body: JSON.stringify(upstreamReq.payload),
    });
  };

  let upstream: Response;
  try {
    upstream = await doFetch();
  } catch (err) {
    console.error("Upstream fetch failed:", err);
    res.status(502).json({
      error: "Failed to reach agent",
      detail: err instanceof Error ? err.message : String(err),
    });
    return;
  }

  // In foundry mode, retry once on 4xx that may indicate a stale session.
  if (
    config.mode === "foundry" &&
    upstreamReq.conversationId &&
    !upstream.ok &&
    (upstream.status === 404 || upstream.status === 409 || upstream.status === 410)
  ) {
    const text = await upstream.text().catch(() => "");
    console.warn(
      `[chat] stale session (${upstream.status}); evicting and retrying once: ${text.slice(0, 300)}`,
    );
    evictSession(upstreamReq.conversationId);
    try {
      upstreamReq = await buildUpstreamRequest(body, input);
      upstream = await doFetch();
    } catch (err) {
      console.error("Retry after stale session failed:", err);
      res.status(502).json({
        error: "Agent retry failed",
        detail: err instanceof Error ? err.message : String(err),
      });
      return;
    }
  }

  if (!upstream.ok || !upstream.body) {
    const text = await upstream.text().catch(() => "");
    console.error(`Upstream error ${upstream.status}: ${text}`);
    res.status(upstream.status || 502).json({
      error: `Agent returned ${upstream.status}`,
      detail: text.slice(0, 2000),
    });
    return;
  }

  // Stream SSE through to the client.
  res.status(200);
  res.setHeader("Content-Type", "text/event-stream");
  res.setHeader("Cache-Control", "no-cache, no-transform");
  res.setHeader("Connection", "keep-alive");
  res.setHeader("X-Accel-Buffering", "no");
  res.flushHeaders?.();

  const reader = upstream.body.getReader();
  const aborted = { done: false };
  req.on("close", () => {
    aborted.done = true;
    reader.cancel().catch(() => {});
  });

  try {
    while (!aborted.done) {
      const { value, done } = await reader.read();
      if (done) break;
      if (value) {
        res.write(Buffer.from(value));
      }
    }
  } catch (err) {
    console.error("Stream relay error:", err);
    if (!res.writableEnded) {
      res.write(
        `event: error\ndata: ${JSON.stringify({
          message: err instanceof Error ? err.message : String(err),
        })}\n\n`,
      );
    }
  } finally {
    if (!res.writableEnded) {
      res.end();
    }
  }
});

/**
 * Reset endpoint: deletes the foundry session bound to this conversation_id.
 * No-op (200) in local mode.
 */
app.post("/api/reset", async (req, res) => {
  const body = req.body as { conversation_id?: string };
  if (config.mode !== "foundry") {
    res.json({ ok: true, mode: config.mode });
    return;
  }
  if (!isUuid(body?.conversation_id)) {
    res.status(400).json({ error: "conversation_id (UUID) is required" });
    return;
  }
  try {
    await deleteSession(body.conversation_id);
    res.json({ ok: true });
  } catch (err) {
    console.error("Reset failed:", err);
    res.status(500).json({
      error: "Failed to delete session",
      detail: err instanceof Error ? err.message : String(err),
    });
  }
});

const server = app.listen(config.port, () => {
  console.log(
    `[webchat-proxy] mode=${config.mode} agentUrl=${config.agentUrl} model=${config.modelName} listening on :${config.port}`,
  );
});

// Attach the voice WebSocket relay (`/api/voice`) to the same HTTP server so it
// shares the Vite dev proxy and production origin. No-op when voice disabled.
attachVoiceRelay(server);
