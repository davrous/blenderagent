import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";
import type { Request, Response, NextFunction } from "express";

export const MEDIA_PREFIX = "BLENDER_MEDIA_V1:";
export const JOB_ID_RE = /^[0-9a-f]{32}$/;
export const COOKIE_NAME = "blender_media_browser";

export function isUuid(value: unknown): value is string {
  return typeof value === "string" && /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value);
}

export function mediaEnabled(secret: string): boolean {
  return secret.length >= 32;
}

function mac(value: string, secret: string): string {
  return createHmac("sha256", secret).update(value).digest("hex");
}

export function signBrowserKey(key: string, secret: string): string {
  return `${key}.${mac(`browser:${key}`, secret)}`;
}

export function verifyBrowserKey(cookie: string, secret: string): string | null {
  const match = /^([0-9a-f]{64})\.([0-9a-f]{64})$/.exec(cookie);
  if (!match) return null;
  const expected = mac(`browser:${match[1]}`, secret);
  return timingSafeEqual(Buffer.from(match[2], "hex"), Buffer.from(expected, "hex")) ? match[1] : null;
}

export function browserKey(req: Request, res: Response, secret: string, secure: boolean): string {
  const cookie = (req.headers.cookie ?? "").split(";").map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE_NAME}=`))?.slice(COOKIE_NAME.length + 1) ?? "";
  const existing = verifyBrowserKey(cookie, secret);
  if (existing) return existing;
  const key = randomBytes(32).toString("hex");
  res.cookie(COOKIE_NAME, signBrowserKey(key, secret), {
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
};

export function signEnvelope(secret: string, scope: string, text: string, references?: string[], action?: MediaAction): string {
  if (!mediaEnabled(secret) || !JOB_ID_RE.test(scope)) throw new Error("Media controls are unavailable");
  const part = Buffer.from(JSON.stringify({ scope, text, ...(references?.length ? { references } : {}), ...(action ? { action } : {}) }), "utf8").toString("base64url");
  return `${MEDIA_PREFIX}${part}.${mac(part, secret)}`;
}

export function voiceMediaContext(cookieHeader: string | undefined, conversationId: string | undefined, secret: string): string | undefined {
  if (!mediaEnabled(secret) || !isUuid(conversationId)) return undefined;
  const cookie = (cookieHeader ?? "").split(";").map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE_NAME}=`))?.slice(COOKIE_NAME.length + 1) ?? "";
  const key = verifyBrowserKey(cookie, secret);
  return key ? signEnvelope(secret, scopeFor(key, conversationId), "Voice request follows.") : undefined;
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