export interface MusicRequest {
  candidate: {
    film: string | null;
    track: string | null;
    confidence: "low" | "medium" | "high" | "unknown";
    evidence: string;
  };
  action: "discuss" | "generate";
  duration_seconds?: 5 | 10 | 15;
  render_mode?: "clay" | "standard";
}

export function parseMusicRequest(value: unknown): MusicRequest | undefined {
  if (value === undefined) return undefined;
  if (!value || typeof value !== "object" || !("candidate" in value) || !("action" in value)) {
    throw new Error("Invalid music confirmation");
  }
  const { candidate, action } = value;
  const duration = "duration_seconds" in value ? value.duration_seconds : 15;
  const renderMode = "render_mode" in value ? value.render_mode : "standard";
  if ((duration !== 5 && duration !== 10 && duration !== 15) || (renderMode !== "clay" && renderMode !== "standard")) {
    throw new Error("Choose 5, 10 or 15 seconds and clay or standard rendering");
  }
  if (action !== "discuss" && action !== "generate") throw new Error("Invalid music action");
  if (!candidate || typeof candidate !== "object" || !("film" in candidate)
    || !("track" in candidate) || !("confidence" in candidate) || !("evidence" in candidate)) {
    throw new Error("Invalid music candidate");
  }
  const { film, track, confidence, evidence } = candidate;
  if ((film !== null && (typeof film !== "string" || !film.trim() || film.length > 200))
    || (track !== null && (typeof track !== "string" || !track.trim() || track.length > 200))
    || (confidence !== "low" && confidence !== "medium" && confidence !== "high" && confidence !== "unknown")
    || typeof evidence !== "string" || !evidence.trim() || evidence.length > 1500
    || (film === null) !== (confidence === "unknown") || (action === "generate" && film === null)) {
    throw new Error("Invalid music recognition result");
  }
  return { action, candidate: { film, track, confidence, evidence }, duration_seconds: duration, render_mode: renderMode };
}
