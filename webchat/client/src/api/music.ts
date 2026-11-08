export type MicMode = "voice" | "music";
export type MusicSource = "gpt_audio" | "acrcloud";
export const MUSIC_SOURCES: MusicSource[] = ["gpt_audio", "acrcloud"];
export const MUSIC_SOURCE_LABELS: Record<MusicSource, string> = { gpt_audio: "GPT Audio", acrcloud: "ACRCloud" };
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
  source?: MusicSource;
}

export interface MusicProviderResult {
  candidate: MusicCandidate | null;
  error: string | null;
  metadata: {
    title: string;
    artists: { name: string | null }[];
    album: { name: string | null } | null;
    score: number | string | null;
  } | null;
}

export interface MusicComparison {
  gpt_audio: MusicProviderResult;
  acrcloud: MusicProviderResult;
  summary: string;
  summary_error: string | null;
}

export function hasMusicMatch(candidate: MusicCandidate): boolean {
  return candidate.film !== null || candidate.track !== null;
}

export function isMusicCandidate(value: unknown): value is MusicCandidate {
  if (!value || typeof value !== "object") return false;
  return "film" in value && (value.film === null || typeof value.film === "string")
    && "track" in value && (value.track === null || typeof value.track === "string")
    && "confidence" in value && typeof value.confidence === "string"
    && ["low", "medium", "high", "unknown"].includes(value.confidence)
    && (value.film === null && value.track === null) === (value.confidence === "unknown")
    && (value.film === null || (value.film.trim().length > 0 && value.film.length <= 200))
    && (value.track === null || (value.track.trim().length > 0 && value.track.length <= 200))
    && "evidence" in value && typeof value.evidence === "string"
    && value.evidence.trim().length > 0 && value.evidence.length <= 1500;
}

function isMusicProviderResult(value: unknown): value is MusicProviderResult {
  if (!value || typeof value !== "object" || !("candidate" in value) || !("error" in value) || !("metadata" in value)) return false;
  if (value.candidate === null ? typeof value.error !== "string" || !value.error.trim() || value.error.length > 500
    : !isMusicCandidate(value.candidate) || value.error !== null) return false;
  const metadata = value.metadata;
  if (metadata === null) return true;
  if (!metadata || typeof metadata !== "object") return false;
  const isName = (name: unknown): name is { name: string | null } => !!name && typeof name === "object"
    && "name" in name && (name.name === null || (typeof name.name === "string" && name.name.length <= 200));
  return "title" in metadata && typeof metadata.title === "string" && !!metadata.title.trim() && metadata.title.length <= 200
    && "artists" in metadata && Array.isArray(metadata.artists) && metadata.artists.length <= 50 && metadata.artists.every(isName)
    && "album" in metadata && (metadata.album === null || isName(metadata.album))
    && "score" in metadata && (metadata.score === null || typeof metadata.score === "string"
      || (typeof metadata.score === "number" && Number.isFinite(metadata.score)));
}

export function isMusicComparison(value: unknown): value is MusicComparison {
  return !!value && typeof value === "object"
    && "gpt_audio" in value && isMusicProviderResult(value.gpt_audio)
    && "acrcloud" in value && isMusicProviderResult(value.acrcloud)
    && "summary" in value && typeof value.summary === "string" && value.summary.length <= 2000
    && "summary_error" in value && (value.summary_error === null || typeof value.summary_error === "string");
}

export function isMusicConfirmation(text: string): boolean {
  const normalized = text.trim().toLowerCase().replace(/[.!?]+$/, "").trim();
  return /^(oui|yes|ok|d'accord)(,?\s+(s'il te plaît|please|vas-y|go ahead|crée la scène devfest|génère la scène devfest))?$/.test(normalized);
}
