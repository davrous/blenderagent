import { WebSocketServer, WebSocket, type RawData } from "ws";
import type { Server, IncomingMessage } from "node:http";
import type { Socket } from "node:net";
import { config } from "./config.js";
import { getBearerToken } from "./auth.js";

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

function buildFoundryVoiceWsUrl(conversationId: string): string {
  // Per the `invocations_ws` protocol (Foundry voice-agent docs + foundry-
  // samples): the project and agent are PATH segments (mirroring the Responses
  // URL), `api-version` is REQUIRED, and `agent_session_id` is optional. The
  // bearer token is sent as an Authorization header on the upgrade. The
  // container serves this route on the same agentserver port as /responses.
  const base = `${config.foundryAgentBase}/endpoint/protocols/invocations_ws`
    .replace(/^https:/i, "wss:")
    .replace(/^http:/i, "ws:");
  const qs = new URLSearchParams({
    "api-version": config.apiVersion,
    agent_session_id: conversationId,
  });
  return `${base}?${qs.toString()}`;
}

async function openUpstream(conversationId: string | undefined): Promise<WebSocket> {
  if (config.mode === "local") {
    return new WebSocket(config.localVoiceWsUrl);
  }
  // Foundry: the container serves `/invocations_ws` on the same agentserver
  // port as the Responses API (the platform proxies the upgrade there). The
  // conversation id rides as `agent_session_id`; scene continuity stays
  // client-owned via the control frames. Authenticate the upgrade with a
  // bearer token (no session pre-creation is required for invocations_ws).
  if (!conversationId) {
    throw new Error("sessionId (conversation UUID) is required for voice in foundry mode");
  }
  const token = await getBearerToken();
  const headers: Record<string, string> = {};
  if (token) headers.Authorization = `Bearer ${token}`;
  return new WebSocket(buildFoundryVoiceWsUrl(conversationId), { headers });
}

function relay(browser: WebSocket, upstream: WebSocket): void {
  const pending: Array<{ data: RawData; isBinary: boolean }> = [];
  let upstreamOpen = false;

  browser.on("message", (data: RawData, isBinary: boolean) => {
    if (upstreamOpen) sendTo(upstream, data, isBinary);
    else pending.push({ data, isBinary });
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

  let upstream: WebSocket;
  try {
    console.log(
      `[voice] browser connected; opening upstream (conversation=${conversationId ?? "none"}, mode=${config.mode})`,
    );
    upstream = await openUpstream(conversationId);
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

  relay(browser, upstream);
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
