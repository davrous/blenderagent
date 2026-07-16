import { WebSocketServer, WebSocket, type RawData } from "ws";
import type { Server, IncomingMessage } from "node:http";
import type { Socket } from "node:net";
import { config } from "./config.js";
import { getBearerToken } from "./auth.js";
import { getOrCreateSession } from "./sessions.js";

const VOICE_PATH = "/api/voice";

/**
 * Bidirectional relay between the browser voice WebSocket and the agent's
 * voice WebSocket (`invocations_ws` protocol).
 *
 * The scene continuity model is *client-owned*: the browser embeds
 * `conversation_id` and `previous_response_id` in its control frames, so the
 * relay is a near-transparent pipe. Its only responsibilities are:
 *   - routing the `/api/voice` upgrade to a WebSocket,
 *   - choosing the upstream target (local agent vs. deployed Foundry agent),
 *   - injecting Foundry auth (bearer token + Foundry-Features) and a session.
 */

function normalizeWsCode(code: number | undefined): number | undefined {
  // Valid application close codes are 1000 or 3000-4999. Anything else
  // (e.g. 1005/1006 "no status") must not be echoed back verbatim.
  if (typeof code !== "number") return undefined;
  if (code === 1000) return 1000;
  if (code >= 3000 && code <= 4999) return code;
  return 1000;
}

function safeClose(ws: WebSocket, code?: number, reason?: string): void {
  try {
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      ws.close(normalizeWsCode(code), reason);
    }
  } catch {
    /* ignore */
  }
}

function sendTo(ws: WebSocket, data: RawData, isBinary: boolean): void {
  if (ws.readyState !== WebSocket.OPEN) return;
  try {
    if (isBinary) {
      ws.send(data, { binary: true });
    } else {
      const text = Array.isArray(data)
        ? Buffer.concat(data).toString("utf-8")
        : data.toString();
      ws.send(text, { binary: false });
    }
  } catch {
    /* ignore transient send errors */
  }
}

/**
 * Inject the shared Foundry `agent_session_id` into browser control frames so
 * the container voice pipeline threads each turn into the SAME server-side
 * session as the text path (unified conversation: portal traces + cross-modal
 * memory). Binary (audio) frames and non-JSON text pass through untouched.
 */
function injectSession(
  data: RawData,
  isBinary: boolean,
  foundryAgentSessionId: string | undefined,
): { data: RawData; isBinary: boolean } {
  if (isBinary || !foundryAgentSessionId) return { data, isBinary };
  try {
    const text = Array.isArray(data)
      ? Buffer.concat(data).toString("utf-8")
      : data.toString();
    const obj = JSON.parse(text);
    if (obj && typeof obj === "object" && typeof obj.type === "string") {
      obj.foundry_agent_session_id = foundryAgentSessionId;
      return { data: Buffer.from(JSON.stringify(obj)), isBinary: false };
    }
  } catch {
    /* not JSON — pass through */
  }
  return { data, isBinary };
}

function buildFoundryVoiceWsUrl(routeSessionId: string): string {
  // Per the `invocations_ws` protocol (Foundry voice-agent docs + foundry-
  // samples): the project and agent are PATH segments (mirroring the Responses
  // URL), `api-version` is REQUIRED, and `agent_session_id` is optional. The
  // bearer token is sent as an Authorization header on the upgrade. The
  // container serves this route on the same agentserver port as /responses.
  //
  // `routeSessionId` is the SHARED Foundry `agent_session_id` (same one the text
  // path uses) when available, so the gateway routes voice to the SAME
  // container/session as text — unifying the conversation. Falls back to the
  // raw conversation id when no Foundry session could be created.
  const base = `${config.foundryAgentBase}/endpoint/protocols/invocations_ws`
    .replace(/^https:/i, "wss:")
    .replace(/^http:/i, "ws:");
  const qs = new URLSearchParams({
    "api-version": config.apiVersion,
    agent_session_id: routeSessionId,
  });
  return `${base}?${qs.toString()}`;
}

async function openUpstream(
  conversationId: string | undefined,
  foundryAgentSessionId?: string,
): Promise<WebSocket> {
  if (config.mode === "local") {
    return new WebSocket(config.localVoiceWsUrl);
  }
  // Foundry: the container serves `/invocations_ws` on the same agentserver
  // port as the Responses API (the platform proxies the upgrade there).
  // Authenticate the upgrade with a bearer token (no session pre-creation is
  // required for invocations_ws). Route to the SHARED Foundry session when we
  // have it so voice lands on the SAME container/conversation as text.
  if (!conversationId) {
    throw new Error("sessionId (conversation UUID) is required for voice in foundry mode");
  }
  const token = await getBearerToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  const routeSessionId = foundryAgentSessionId ?? conversationId;
  return new WebSocket(buildFoundryVoiceWsUrl(routeSessionId), { headers });
}

function relay(
  browser: WebSocket,
  upstream: WebSocket,
  foundryAgentSessionId?: string,
): void {
  const pending: Array<{ data: RawData; isBinary: boolean }> = [];
  let upstreamOpen = false;

  browser.on("message", (data: RawData, isBinary: boolean) => {
    const out = injectSession(data, isBinary, foundryAgentSessionId);
    if (upstreamOpen) sendTo(upstream, out.data, out.isBinary);
    else pending.push({ data: out.data, isBinary: out.isBinary });
  });

  upstream.on("open", () => {
    upstreamOpen = true;
    console.log("[voice] upstream connected");
    while (pending.length) {
      const m = pending.shift()!;
      sendTo(upstream, m.data, m.isBinary);
    }
  });

  upstream.on("message", (data: RawData, isBinary: boolean) => {
    // Surface agent-side error frames in the server log (they otherwise only
    // reach the browser and show as a generic bubble). Cheap: text frames only.
    if (!isBinary) {
      try {
        const text = Array.isArray(data)
          ? Buffer.concat(data).toString("utf-8")
          : data.toString();
        if (text.includes('"error"')) {
          const obj = JSON.parse(text);
          if (obj && obj.type === "error") {
            console.error("[voice] agent error frame:", obj.message ?? text.slice(0, 400));
          }
        }
      } catch {
        /* not JSON — ignore */
      }
    }
    sendTo(browser, data, isBinary);
  });

  const closeBoth = (code?: number, reason?: string) => {
    safeClose(browser, code, reason);
    safeClose(upstream, code, reason);
  };

  browser.on("close", (code, reason) => closeBoth(code, reason?.toString()));
  upstream.on("close", (code, reason) => {
    if (code && code !== 1000) {
      console.warn(`[voice] upstream closed code=${code} reason=${reason?.toString() || ""}`);
    }
    closeBoth(code, reason?.toString());
  });
  browser.on("error", () => closeBoth(1011, "browser error"));
  upstream.on("error", (err) => {
    console.error("[voice] upstream WS error:", err instanceof Error ? err.message : err);
    // Tell the browser before tearing down so it can surface a friendly error.
    sendTo(
      browser,
      Buffer.from(
        JSON.stringify({ type: "error", message: "Voice service is unavailable." }),
      ),
      false,
    );
    closeBoth(1011, "upstream error");
  });
}

async function handleVoiceConnection(browser: WebSocket, req: IncomingMessage): Promise<void> {
  let conversationId: string | undefined;
  try {
    const url = new URL(req.url ?? "/", "http://localhost");
    conversationId = url.searchParams.get("sessionId") ?? undefined;
  } catch {
    /* ignore */
  }

  // Resolve the shared Foundry session BEFORE opening the upstream. This MUST
  // happen first for two reasons:
  //   1. Awaiting it AFTER openUpstream() lets the upstream 'open' event fire
  //      during the await — before relay() attaches its listener — so the relay
  //      never flushes buffered frames and voice is silently dead (until a
  //      second attempt warms the session cache and shrinks the race window).
  //   2. We route the voice WS to this same session id so voice and text land
  //      on ONE container / ONE Foundry conversation.
  // getOrCreateSession caches by conversation id, so text and voice resolve to
  // the SAME agent_session_id. If it fails, voice still works via the
  // container's inline-history fallback (just not unified).
  let foundryAgentSessionId: string | undefined;
  if (config.mode === "foundry" && conversationId) {
    try {
      const { agentSessionId } = await getOrCreateSession(conversationId);
      foundryAgentSessionId = agentSessionId;
      console.log(
        `[voice] threading into foundry session ${agentSessionId} (conversation=${conversationId})`,
      );
    } catch (err) {
      console.warn(
        "[voice] could not resolve foundry session; falling back to inline history:",
        err instanceof Error ? err.message : err,
      );
    }
  }

  let upstream: WebSocket;
  try {
    console.log(
      `[voice] browser connected; opening upstream (conversation=${conversationId ?? "none"}, session=${foundryAgentSessionId ?? "none"}, mode=${config.mode})`,
    );
    upstream = await openUpstream(conversationId, foundryAgentSessionId);
  } catch (err) {
    console.error("[voice] failed to open upstream:", err instanceof Error ? err.message : err);
    sendTo(
      browser,
      Buffer.from(
        JSON.stringify({
          type: "error",
          message: err instanceof Error ? err.message : "Voice unavailable.",
        }),
      ),
      false,
    );
    safeClose(browser, 1011, "upstream open failed");
    return;
  }

  relay(browser, upstream, foundryAgentSessionId);
}

/**
 * Attach the `/api/voice` WebSocket relay to an existing HTTP server.
 * No-op registration when voice is disabled (upgrades on `/api/voice` are
 * rejected so the client falls back to text-only).
 */
export function attachVoiceRelay(server: Server): void {
  const voiceWss = new WebSocketServer({ noServer: true });

  server.on("upgrade", (req: IncomingMessage, socket: Socket, head: Buffer) => {
    let pathname = "/";
    try {
      pathname = new URL(req.url ?? "/", "http://localhost").pathname;
    } catch {
      /* ignore */
    }
    if (pathname !== VOICE_PATH) {
      // Not ours — leave other upgrade handlers a chance, but if none exist the
      // socket must be closed to avoid a hanging connection.
      return;
    }
    if (!config.voiceEnabled) {
      socket.destroy();
      return;
    }
    voiceWss.handleUpgrade(req, socket, head, (ws) => {
      voiceWss.emit("connection", ws, req);
    });
  });

  voiceWss.on("connection", (browser: WebSocket, req: IncomingMessage) => {
    void handleVoiceConnection(browser, req);
  });

  console.log(
    `[voice] relay attached at ${VOICE_PATH} (enabled=${config.voiceEnabled}, mode=${config.mode})`,
  );
}
