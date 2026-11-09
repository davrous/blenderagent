import { createHash, randomBytes } from "node:crypto";
import type { Request, Response, NextFunction } from "express";

export const JOB_ID_RE = /^[0-9a-f]{32}$/;
export const COOKIE_NAME = "blender_media_browser";

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export function parseBrowserKey(cookie: string): string | null {
  // Retain the random key from old signed cookies so existing jobs remain owned.
  return /^([0-9a-f]{64})(?:\.[0-9a-f]{64})?$/.exec(cookie)?.[1] ?? null;
}

export function browserKey(req: Request, res: Response, secure: boolean): string {
  const cookie = (req.headers.cookie ?? "").split(";").map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE_NAME}=`))?.slice(COOKIE_NAME.length + 1) ?? "";
  const existing = parseBrowserKey(cookie);
  if (existing) return existing;
  const key = randomBytes(32).toString("hex");
  res.cookie(COOKIE_NAME, key, {
    httpOnly: true, sameSite: "strict", secure, path: "/", maxAge: 365 * 24 * 60 * 60 * 1000,
  });
  return key;
}

export function scopeFor(key: string, conversationId: string): string {
  if (!isUuid(conversationId)) throw new Error("conversation_id (UUID) is required");
  return createHash("sha256").update(`${key}:${conversationId.toLowerCase()}`).digest("hex").slice(0, 32);
}

export type MediaAction = {
  type: "status" | "approve" | "cancel";
  job_id: string;
  prompt?: string;
  resolution?: "480p" | "720p" | "1080p" | "4k";
  generate_audio?: boolean;
  consent?: boolean;
};

export type MediaContext = { scope: string; references?: string[]; action?: Omit<MediaAction, "prompt"> };

export function mediaMetadata(context: MediaContext): string {
  if (!JOB_ID_RE.test(context.scope)) throw new Error("Invalid media scope");
  const text = JSON.stringify(context);
  if (text.length > 512) throw new Error("Media metadata exceeds the Responses limit");
  return text;
}

export function voiceMediaContext(cookieHeader: string | undefined, conversationId: string | undefined): string | undefined {
  if (!isUuid(conversationId)) return undefined;
  const cookie = (cookieHeader ?? "").split(";").map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE_NAME}=`))?.slice(COOKIE_NAME.length + 1) ?? "";
  const key = parseBrowserKey(cookie);
  return key ? mediaMetadata({ scope: scopeFor(key, conversationId) }) : undefined;
}

export function ownedReferences(value: unknown, scope: string): string[] {
  if (value === undefined) return [];
  if (!Array.isArray(value) || value.length > 4) throw new Error("At most four reference IDs are allowed");
  const pattern = new RegExp(`^references/${scope}/[0-9a-f]{32}\\.(png|jpg|webp|mp4)$`);
  if (!value.every((item) => typeof item === "string" && pattern.test(item))) {
    throw new Error("Reference does not belong to this browser and conversation");
  }
  return [...new Set(value)] as string[];
}

export function requireOrigin(origin: string) {
  return (req: Request, res: Response, next: NextFunction): void => {
    if (req.headers.origin !== new URL(origin).origin) {
      res.status(403).json({ error: "Same-origin request required" });
      return;
    }
    next();
  };
}