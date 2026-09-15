export const JOB_ID_RE = /^[0-9a-f]{32}$/;
export const TERMINAL_JOB_STATES = new Set(["completed", "failed", "cancelled", "submission_unknown"]);
export const VIDEO_JOB_STATES = new Set(["queued", "rendering", "encoding", "awaiting_seedance_approval", "wavespeed_uploading", "wavespeed_submitting", "wavespeed_processing", ...TERMINAL_JOB_STATES]);
export const RESOLUTION_RATES = { "480p": 0.11, "720p": 0.22, "1080p": 0.55, "4k": 1.10 };
export type VideoResolution = keyof typeof RESOLUTION_RATES;
export interface VideoJob {
  id: string;
  state: string;
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
export interface ReferenceUpload {
  blob_name: string;
  name: string;
  media_type: string;
  metadata_validated?: boolean;
}
export interface ApprovalOptions {
  prompt: string;
  resolution: VideoResolution;
  generate_audio: boolean;
}

export function paidEstimate(inputSeconds: number, outputSeconds: number, resolution: VideoResolution): number {
  return (inputSeconds + outputSeconds) * RESOLUTION_RATES[resolution];
}

export function blobProxyUrl(url: string): string {
  return `/api/blob?url=${encodeURIComponent(url)}`;
}

const requests = new Map<string, { action: string; promise: Promise<VideoJob> }>();

export function requestVideoJob(conversationId: string, id: string, action: "status" | "approve" | "cancel" = "status", options?: ApprovalOptions): Promise<VideoJob> {
  if (!JOB_ID_RE.test(id)) return Promise.reject(new Error("Invalid video job ID"));
  const key = `${conversationId}:${id}`;
  const pending = requests.get(key);
  if (pending) return action === "status" && pending.action === "status" ? pending.promise : Promise.reject(new Error("A job request is already in progress"));
  const promise = (async () => {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 55_000);
    try {
      const suffix = action === "status" ? `?conversation_id=${encodeURIComponent(conversationId)}` : `/${action}`;
      const response = await fetch(`/api/video-jobs/${id}${suffix}`, {
        method: action === "status" ? "GET" : "POST", cache: "no-store", signal: controller.signal,
        ...(action !== "status" ? { headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conversation_id: conversationId, ...options }) } : {}),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || `Job request failed (${response.status})`);
      if (body.id !== id || !VIDEO_JOB_STATES.has(body.state) || typeof body.seedance_enabled !== "boolean" || !Number.isFinite(body.duration_seconds) || body.duration_seconds < 0) throw new Error("Invalid video job response");
      return body as VideoJob;
    } catch (error) {
      if (controller.signal.aborted) throw new Error("Job request timed out. Refresh status before trying an action again.");
      throw error;
    } finally { window.clearTimeout(timer); }
  })();
  requests.set(key, { action, promise });
  void promise.finally(() => { if (requests.get(key)?.promise === promise) requests.delete(key); }).catch(() => {});
  return promise;
}

export function loadJobIds(conversationId: string): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(`webchat.videoJobs.${conversationId}`) ?? "[]");
    return Array.isArray(value) ? [...new Set(value.filter((id): id is string => typeof id === "string" && JOB_ID_RE.test(id)))].slice(-100) : [];
  } catch { return []; }
}

export function saveJobIds(conversationId: string, ids: string[]): void {
  localStorage.setItem(`webchat.videoJobs.${conversationId}`, JSON.stringify(ids.filter((id) => JOB_ID_RE.test(id)).slice(-100)));
}

export function uploadReference(file: File, conversationId: string, signal: AbortSignal, onProgress: (percent: number) => void): Promise<ReferenceUpload> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    const abort = () => request.abort();
    request.open("POST", `/api/references?conversation_id=${encodeURIComponent(conversationId)}`);
    request.timeout = 190_000;
    request.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100)); };
    request.onloadend = () => signal.removeEventListener("abort", abort);
    request.onerror = () => reject(new Error("Upload failed. Check the connection and try again."));
    request.ontimeout = () => reject(new Error("Upload timed out"));
    request.onabort = () => reject(new Error("Upload cancelled"));
    request.onload = () => {
      try {
        const result = JSON.parse(request.responseText);
        if (request.status < 200 || request.status >= 300) throw new Error(result.error || "Upload failed");
        if (typeof result.blob_name !== "string" || !/^references\/[0-9a-f]{32}\/[0-9a-f]{32}\.(png|jpg|webp|mp4)$/.test(result.blob_name)) throw new Error("Invalid upload response");
        resolve(result as ReferenceUpload);
      } catch (error) { reject(error); }
    };
    if (signal.aborted) { reject(new Error("Upload cancelled")); return; }
    signal.addEventListener("abort", abort, { once: true });
    const form = new FormData();
    form.append("file", file);
    request.send(form);
  });
}