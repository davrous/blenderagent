import express from "express";
import cors from "cors";
import { config } from "./config.js";
import { getBearerToken } from "./auth.js";

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use(
  cors({
    origin: config.clientOrigin,
  }),
);

app.get("/api/health", (_req, res) => {
  res.json({
    mode: config.mode,
    agentUrl: config.agentUrl,
    model: config.modelName,
  });
});

interface ChatRequestBody {
  input?: string;
  previous_response_id?: string | null;
}

app.post("/api/chat", async (req, res) => {
  const body = req.body as ChatRequestBody;
  const input = (body.input ?? "").toString();
  if (!input.trim()) {
    res.status(400).json({ error: "input is required" });
    return;
  }

  const upstreamBody: Record<string, unknown> = {
    model: config.modelName,
    input,
    stream: true,
  };
  if (body.previous_response_id) {
    upstreamBody.previous_response_id = body.previous_response_id;
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };

  try {
    const token = await getBearerToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  } catch (err) {
    console.error("Token acquisition failed:", err);
    res.status(500).json({
      error: "Failed to acquire Azure credential",
      detail: err instanceof Error ? err.message : String(err),
    });
    return;
  }

  const upstreamUrl = `${config.agentUrl}/responses`;

  let upstream: Response;
  try {
    upstream = await fetch(upstreamUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(upstreamBody),
    });
  } catch (err) {
    console.error("Upstream fetch failed:", err);
    res.status(502).json({
      error: "Failed to reach agent",
      detail: err instanceof Error ? err.message : String(err),
    });
    return;
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

app.listen(config.port, () => {
  console.log(
    `[webchat-proxy] mode=${config.mode} agentUrl=${config.agentUrl} model=${config.modelName} listening on :${config.port}`,
  );
});
