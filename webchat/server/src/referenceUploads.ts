import Busboy from "busboy";
import { BlobServiceClient } from "@azure/storage-blob";
import { DefaultAzureCredential } from "@azure/identity";
import { execFile } from "node:child_process";
import { createWriteStream } from "node:fs";
import { mkdtemp, open, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { pipeline } from "node:stream/promises";
import type { Request } from "express";

export const MAX_REFERENCE_BYTES = 200 * 1024 * 1024;
const TYPES: Record<string, string> = { "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "video/mp4": "mp4" };

export function sniffMedia(bytes: Buffer): string | null {
  if (bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return "image/png";
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return "image/jpeg";
  if (bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP") return "image/webp";
  if (bytes.toString("ascii", 4, 8) === "ftyp" && /^(isom|iso[2-9]|mp4[12]|avc1|M4V |dash)$/.test(bytes.toString("ascii", 8, 12))) return "video/mp4";
  return null;
}

export interface ProbeMetadata {
  streams?: { codec_type?: string; width?: number; height?: number; duration?: string }[];
  format?: { duration?: string };
}

export function validateMetadata(metadata: ProbeMetadata, mediaType: string): void {
  const stream = metadata.streams?.find((item) => item.codec_type === "video");
  const width = Number(stream?.width);
  const height = Number(stream?.height);
  if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0) throw new Error("Media dimensions are invalid");
  if (mediaType === "video/mp4") {
    const duration = Number(metadata.format?.duration ?? stream?.duration);
    if (width > 1920 || height > 1080 || !Number.isFinite(duration) || duration < 4 || duration > 30) {
      throw new Error("Video must be at most 1920x1080 and 4-30 seconds long");
    }
  } else if (width * height > 16_000_000) {
    throw new Error("Images must be at most 16 megapixels");
  }
}

function probe(file: string, signal: AbortSignal): Promise<ProbeMetadata | null> {
  return new Promise((resolve, reject) => {
    execFile("ffprobe", ["-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries", "stream=codec_type,width,height,duration:format=duration", "-of", "json", file],
      { timeout: 20_000, maxBuffer: 1024 * 1024, windowsHide: true, signal }, (error, stdout) => {
        if (error && "code" in error && error.code === "ENOENT") { resolve(null); return; }
        if (error) { reject(new Error("Media could not be inspected by ffprobe")); return; }
        try { resolve(JSON.parse(stdout) as ProbeMetadata); } catch { reject(new Error("Invalid ffprobe output")); }
      });
  });
}

export async function withReferenceUpload<T>(req: Request, signal: AbortSignal, consume: (file: string, info: { name: string; media_type: string; ext: string; metadata_validated: boolean }) => Promise<T>): Promise<T> {
  const directory = await mkdtemp(path.join(tmpdir(), "blender-reference-"));
  const file = path.join(directory, "reference");
  const writes: Promise<void>[] = [];
  let failure: Error | undefined;
  let name = "";
  let mediaType = "";
  let count = 0;
  try {
    const parser = Busboy({ headers: req.headers, limits: { fileSize: MAX_REFERENCE_BYTES + 1, files: 1, fields: 0, parts: 2 } });
    for (const event of ["filesLimit", "fieldsLimit", "partsLimit"] as const) {
      parser.on(event, () => { failure = new Error("Exactly one file and no other multipart fields are allowed"); });
    }
    parser.on("file", (_field, stream, info) => {
      count++;
      name = path.basename(info.filename.replaceAll("\\", "/")).replace(/[\x00-\x1f]/g, "").slice(0, 200) || "reference";
      mediaType = info.mimeType;
      if (!TYPES[mediaType]) failure = new Error("Only PNG, JPEG, WebP and MP4 are supported");
      stream.on("limit", () => { failure = new Error("Reference exceeds 200 MiB"); });
      writes.push(pipeline(stream, createWriteStream(file, { flags: "wx", mode: 0o600 }), { signal }).catch((error: Error) => { failure = error; }));
    });
    await pipeline(req, parser, { signal });
    await Promise.all(writes);
    if (failure) throw failure;
    if (count !== 1) throw new Error("Exactly one reference file is required");
    const size = (await stat(file)).size;
    if (size === 0 || size > MAX_REFERENCE_BYTES) throw new Error("Reference must be nonempty and at most 200 MiB");
    const handle = await open(file, "r");
    const bytes = Buffer.alloc(64);
    try { await handle.read(bytes, 0, bytes.length, 0); } finally { await handle.close(); }
    if (sniffMedia(bytes) !== mediaType) throw new Error("File signature does not match the declared media type");
    const metadata = await probe(file, signal);
    if (metadata) validateMetadata(metadata, mediaType);
    signal.throwIfAborted();
    return await consume(file, { name, media_type: mediaType, ext: TYPES[mediaType], metadata_validated: metadata !== null });
  } finally {
    await Promise.all(writes);
    await rm(directory, { recursive: true, force: true });
  }
}

export async function storeReference(account: string, blobName: string, file: string, mediaType: string, signal: AbortSignal): Promise<void> {
  if (!/^[a-z0-9]{3,24}$/.test(account)) throw new Error("AZURE_STORAGE_ACCOUNT_NAME is not configured");
  const service = new BlobServiceClient(`https://${account}.blob.core.windows.net`, new DefaultAzureCredential());
  const container = service.getContainerClient("screenshots");
  const properties = await container.getProperties({ abortSignal: signal });
  if (properties.blobPublicAccess) throw new Error("The screenshots container must be private");
  const blob = container.getBlockBlobClient(blobName);
  try {
    await blob.uploadFile(file, { abortSignal: signal, blockSize: 4 * 1024 * 1024, concurrency: 2, conditions: { ifNoneMatch: "*" }, blobHTTPHeaders: { blobContentType: mediaType } });
    signal.throwIfAborted();
  } catch (error) {
    await blob.deleteIfExists({ abortSignal: AbortSignal.timeout(10_000) }).catch(() => {});
    throw error;
  }
}