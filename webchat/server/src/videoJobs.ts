import type { Express, Request, Response } from "express";
import { randomUUID } from "node:crypto";
import { config } from "./config.js";
import { browserKey, isUuid, JOB_ID_RE, mediaEnabled, requireOrigin, scopeFor, signEnvelope, type MediaAction } from "./mediaSecurity.js";
import { storeReference, withReferenceUpload } from "./referenceUploads.js";

export const JOB_STATES = ["queued", "rendering", "encoding", "awaiting_seedance_approval", "wavespeed_uploading", "wavespeed_submitting", "wavespeed_processing", "submission_unknown", "completed", "failed", "cancelled"] as const;
const TERMINAL = new Set(["completed", "failed", "cancelled", "submission_unknown"]);
export interface VideoJob {
  id: string;
  state: typeof JOB_STATES[number];
  progress: number;
  mode: string;
  duration_seconds: number;
  fps: number;
  resolution: string;
  seedance_enabled: boolean;
  preview_url?: string;
  poster_url?: string;
  output_url?: string;
  error?: string;
  estimate_usd?: number;
}

export function safeBlobUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || (url.port && url.port !== "443")) return undefined;
    if (!config.blobProxyAllowedHostSuffixes.some((suffix) => url.hostname === suffix.replace(/^\./, "") || url.hostname.endsWith(`.${suffix.replace(/^\./, "")}`))) return undefined;
    return url.href;
  } catch { return undefined; }
}

export function validateJob(value: unknown, id: string): VideoJob {
  if (!value || typeof value !== "object") throw new Error("Invalid video job descriptor");
  const job = value as Record<string, unknown>;
  if (job.id !== id || !JOB_ID_RE.test(id) || !JOB_STATES.includes(job.state as VideoJob["state"]) || typeof job.seedance_enabled !== "boolean" || typeof job.mode !== "string" || typeof job.resolution !== "string") throw new Error("Invalid video job descriptor");
  for (const field of ["progress", "duration_seconds", "fps"] as const) {
    if (typeof job[field] !== "number" || !Number.isFinite(job[field]) || job[field] < 0) throw new Error("Invalid video job metadata");
  }
  return {
    id, state: job.state as VideoJob["state"], progress: job.progress as number, mode: job.mode.slice(0, 100),
    duration_seconds: job.duration_seconds as number, fps: job.fps as number, resolution: job.resolution.slice(0, 40), seedance_enabled: job.seedance_enabled,
    preview_url: safeBlobUrl(job.preview_url), poster_url: safeBlobUrl(job.poster_url), output_url: safeBlobUrl(job.output_url),
    error: typeof job.error === "string" ? job.error.slice(0, 2000) : undefined,
    estimate_usd: typeof job.estimate_usd === "number" && Number.isFinite(job.estimate_usd) && job.estimate_usd >= 0 ? job.estimate_usd : undefined,
  };
}

export async function readJobStream(response: globalThis.Response, id: string): Promise<VideoJob> {
  if (!response.ok || !response.body) throw new Error(`Agent control returned HTTP ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let text = "";
  let bytes = 0;
  const consume = (frame: string) => {
    const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
    if (!data || data === "[DONE]") return;
    const payload = JSON.parse(data);
    const event = frame.split(/\r?\n/).find((line) => line.startsWith("event:"))?.slice(6).trim() || payload.type;
    if (event === "response.failed" || event === "error" || event === "response.incomplete") throw new Error("Agent control failed; refresh status before any further action");
    if (event === "response.output_text.delta" && typeof payload.delta === "string") text += payload.delta;
  };
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 1024 * 1024) throw new Error("Agent control response is too large");
      buffer += decoder.decode(value, { stream: true });
      let separator: RegExpExecArray | null;
      while ((separator = /\r?\n\r?\n/.exec(buffer))) {
        consume(buffer.slice(0, separator.index));
        buffer = buffer.slice(separator.index + separator[0].length);
      }
    }
    buffer += decoder.decode();
    if (buffer.trim()) consume(buffer);
    const blocks = [...text.matchAll(/```videojob\s*\n([\s\S]*?)```/gi)];
    for (const block of blocks.reverse()) {
      const job = JSON.parse(block[1]);
      if (job?.id === id) return validateJob(job, id);
    }
    throw new Error("Agent did not return the requested video job descriptor");
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

type UpstreamBuilder = (body: { conversation_id?: string }, input: string, control?: boolean) => Promise<{ url: string; headers: Record<string, string>; payload: Record<string, unknown> }>;

export function withDeadline<T>(pending: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error("Job request timed out or was disconnected"));
    if (signal.aborted) abort();
    else signal.addEventListener("abort", abort, { once: true });
    pending.then(resolve, reject).finally(() => signal.removeEventListener("abort", abort));
  });
}

export function requestScope(req: Request, res: Response, conversation: unknown): string {
  if (!isUuid(conversation)) throw new Error("conversation_id (UUID) is required");
  return scopeFor(browserKey(req, res, config.mediaControlSecret, new URL(config.clientOrigin).protocol === "https:"), conversation);
}

export function registerMediaRoutes(app: Express, build: UpstreamBuilder, upload = storeReference): void {
  const inFlight = new Set<string>();
  const uploads = new Set<string>();
  const origin = requireOrigin(config.clientOrigin);
  app.use(["/api/references", "/api/video-jobs"], (_req, res, next) => {
    res.setHeader("Cache-Control", "no-store");
    if (!mediaEnabled(config.mediaControlSecret)) { res.status(503).json({ error: "Media disabled: MEDIA_CONTROL_SECRET must contain at least 32 characters" }); return; }
    next();
  });

  app.post("/api/references", origin, async (req, res) => {
    let scope = "";
    let acquired = false;
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 180_000);
    res.on("close", () => { if (!res.writableEnded) controller.abort(); });
    try {
      scope = requestScope(req, res, req.query.conversation_id);
      if (uploads.has(scope)) { res.status(409).json({ error: "An upload is already in progress" }); return; }
      uploads.add(scope);
      acquired = true;
      const result = await withReferenceUpload(req, controller.signal, async (file, info) => {
        const blobName = `references/${scope}/${randomUUID().replaceAll("-", "")}.${info.ext}`;
        await upload(config.storageAccountName, blobName, file, info.media_type, controller.signal);
        return { blob_name: blobName, name: info.name, media_type: info.media_type, metadata_validated: info.metadata_validated };
      });
      res.status(201).json(result);
    } catch (error) {
      if (!res.destroyed) res.status(controller.signal.aborted ? 408 : 400).json({ error: error instanceof Error ? error.message : "Upload failed" });
    } finally {
      clearTimeout(timer);
      if (acquired) uploads.delete(scope);
    }
  });

  const control = async (req: Request, res: Response, type: MediaAction["type"]) => {
    const conversation = type === "status" ? req.query.conversation_id : req.body?.conversation_id;
    const id = String(req.params.id);
    if (!JOB_ID_RE.test(id) || !isUuid(conversation)) { res.status(400).json({ error: "Valid job ID and conversation_id are required" }); return; }
    const scope = requestScope(req, res, conversation);
    const lock = `${scope}:${id}`;
    if (inFlight.has(lock)) { res.status(409).json({ error: "Job request already in progress; retry status shortly" }); return; }
    let action: MediaAction = { type, job_id: id };
    if (type === "approve") {
      const { prompt = "", resolution = "720p", generate_audio = false } = req.body;
      if (typeof prompt !== "string" || prompt.length > 8000 || !["480p", "720p", "1080p", "4k"].includes(resolution) || typeof generate_audio !== "boolean") { res.status(400).json({ error: "Invalid approval options" }); return; }
      action = { ...action, prompt, resolution, generate_audio };
    }
    inFlight.add(lock);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 45_000);
    res.on("close", () => { if (!res.writableEnded) controller.abort(); });
    const call = async (operation: MediaAction) => {
      const upstream = await withDeadline(build({ conversation_id: conversation }, signEnvelope(config.mediaControlSecret, scope, "", undefined, operation), true), controller.signal);
      controller.signal.throwIfAborted();
      const response = await fetch(upstream.url, { method: "POST", headers: upstream.headers, body: JSON.stringify(upstream.payload), signal: controller.signal });
      return readJobStream(response, id);
    };
    try {
      if (type !== "status") {
        const live = await call({ type: "status", job_id: id });
        if ((type === "approve" && (live.state !== "awaiting_seedance_approval" || !live.seedance_enabled)) || (type === "cancel" && TERMINAL.has(live.state))) {
          res.status(409).json({ error: "Job is no longer eligible for this action. Refresh status.", job: live });
          return;
        }
      }
      res.json(await call(action));
    } catch (error) {
      if (!res.destroyed) res.status(controller.signal.aborted ? 504 : 502).json({ error: error instanceof Error ? error.message : "Job control failed" });
    } finally {
      clearTimeout(timer);
      inFlight.delete(lock);
    }
  };
  app.get("/api/video-jobs/:id", (req, res) => { void control(req, res, "status"); });
  app.post("/api/video-jobs/:id/approve", origin, (req, res) => { void control(req, res, "approve"); });
  app.post("/api/video-jobs/:id/cancel", origin, (req, res) => { void control(req, res, "cancel"); });
}