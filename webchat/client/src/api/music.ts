export type MicMode = "voice" | "music";
export const MUSIC_DURATIONS = [5, 10, 15] as const;
export interface MusicGenerationOptions {
  duration_seconds: typeof MUSIC_DURATIONS[number];
  render_mode: "clay" | "standard";
}

export interface MusicCandidate {
  film: string | null;
  track: string | null;
  confidence: "low" | "medium" | "high" | "unknown";
  evidence: string;
}

export interface MusicRequest {
  candidate: MusicCandidate;
  action: "discuss" | "generate";
  duration_seconds?: MusicGenerationOptions["duration_seconds"];
  render_mode?: MusicGenerationOptions["render_mode"];
}

export function isMusicCandidate(value: unknown): value is MusicCandidate {
  if (!value || typeof value !== "object") return false;
  return "film" in value && (value.film === null || typeof value.film === "string")
    && "track" in value && (value.track === null || typeof value.track === "string")
    && "confidence" in value && typeof value.confidence === "string"
    && ["low", "medium", "high", "unknown"].includes(value.confidence)
    && (value.film === null) === (value.confidence === "unknown")
    && (value.film === null || (value.film.trim().length > 0 && value.film.length <= 200))
    && (value.track === null || (value.track.trim().length > 0 && value.track.length <= 200))
    && "evidence" in value && typeof value.evidence === "string"
    && value.evidence.trim().length > 0 && value.evidence.length <= 1500;
}

export function isMusicConfirmation(text: string): boolean {
  const normalized = text.trim().toLowerCase().replace(/[.!?]+$/, "").trim();
  return /^(oui|yes|ok|d'accord)(,?\s+(s'il te plaît|please|vas-y|go ahead|crée la scène devfest|génère la scène devfest))?$/.test(normalized);
}
