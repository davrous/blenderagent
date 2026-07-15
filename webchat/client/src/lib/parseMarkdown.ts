/** Heuristic: does this URL look like a downloadable Blender scene file? */
export function isDownloadLink(href: string): boolean {
  try {
    const u = new URL(href);
    const path = u.pathname.toLowerCase();
    return (
      path.endsWith(".blend") ||
      path.endsWith(".glb") ||
      path.endsWith(".gltf") ||
      path.endsWith(".fbx")
    );
  } catch {
    return false;
  }
}

export function filenameFromUrl(href: string): string {
  try {
    const u = new URL(href);
    const last = u.pathname.split("/").filter(Boolean).pop() ?? "download";
    return decodeURIComponent(last);
  } catch {
    return "download";
  }
}

// Matches a markdown image `![alt](url)` or link `[text](url)`, capturing the
// leading `!` (group 1) and the url (group 2). Global + multiline so we can walk
// every occurrence in a message.
const MEDIA_RE = /(!?)\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)/g;

/**
 * Remove duplicate markdown media (images and download links) from a single
 * assistant message, keeping only the first occurrence of each URL.
 *
 * Rationale: the agent emits some media twice — once via the server's early
 * "surface" of a tool result (so the card/image shows immediately) and once
 * again in the model's final text (the tool result asks the model to echo it).
 * Both land in the same message buffer, so without this the client renders two
 * identical download cards / images. Dedup is scoped per-message and keyed on
 * the blob URL (which carries a unique timestamp+uuid), so distinct files never
 * collide and a legitimately different link is never suppressed.
 *
 * Only image (`![…](url)`) and download links (.blend/.glb/.gltf/.fbx) are
 * deduplicated; ordinary text links are left untouched.
 */
export function dedupeMarkdownMedia(text: string): string {
  if (!text) return text;
  const seen = new Set<string>();
  let result = "";
  let lastIndex = 0;
  for (const match of text.matchAll(MEDIA_RE)) {
    const isImage = match[1] === "!";
    const url = match[2];
    // Only consider media we actually render as cards/images.
    if (!isImage && !isDownloadLink(url)) continue;
    const start = match.index ?? 0;
    if (seen.has(url)) {
      // Drop this duplicate occurrence; keep the text before it.
      result += text.slice(lastIndex, start);
      lastIndex = start + match[0].length;
    } else {
      seen.add(url);
    }
  }
  result += text.slice(lastIndex);
  return result;
}

// ── Asset galleries (```models / ```textures fenced blocks) ────────────────
// The agent emits the raw JSON output of `list_available_models` /
// `list_available_textures` verbatim inside a fenced block tagged `models` or
// `textures`. We parse those into clickable thumbnail galleries and strip the
// blocks from the prose so the JSON is never shown.

export interface GalleryModel {
  name: string;
  imageUrl: string;
  modelUrl: string;
}

export interface GalleryTexture {
  name: string;
  imageUrl: string;
  assetId: string;
}

export type Gallery =
  | { kind: "models"; items: GalleryModel[] }
  | { kind: "textures"; items: GalleryTexture[] };

const MODELS_BLOCK_RE = /```models\s*\n([\s\S]*?)```/gi;
const TEXTURES_BLOCK_RE = /```textures\s*\n([\s\S]*?)```/gi;

function parseJsonArray(raw: string): any[] {
  try {
    const parsed = JSON.parse(raw.trim());
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

/**
 * Extract every ```models / ```textures gallery from an assistant message, in
 * document order. Only complete (closed) fenced blocks with valid JSON are
 * returned, so a block that is still streaming is ignored until it finishes.
 */
export function extractGalleries(text: string): Gallery[] {
  if (!text) return [];
  const galleries: { index: number; gallery: Gallery }[] = [];

  for (const m of text.matchAll(MODELS_BLOCK_RE)) {
    const items: GalleryModel[] = [];
    for (const item of parseJsonArray(m[1])) {
      if (item && item.name && item.imageUrl && item.modelUrl) {
        items.push({
          name: String(item.name),
          imageUrl: String(item.imageUrl),
          modelUrl: String(item.modelUrl),
        });
      }
    }
    if (items.length) galleries.push({ index: m.index ?? 0, gallery: { kind: "models", items } });
  }

  for (const m of text.matchAll(TEXTURES_BLOCK_RE)) {
    const items: GalleryTexture[] = [];
    for (const item of parseJsonArray(m[1])) {
      if (item && item.name && item.imageUrl && item.assetId) {
        items.push({
          name: String(item.name),
          imageUrl: String(item.imageUrl),
          assetId: String(item.assetId),
        });
      }
    }
    if (items.length) galleries.push({ index: m.index ?? 0, gallery: { kind: "textures", items } });
  }

  return galleries.sort((a, b) => a.index - b.index).map((g) => g.gallery);
}

/**
 * Remove ```models / ```textures fenced blocks from the prose so the raw JSON
 * is never rendered. Also drops an UNCLOSED trailing `models`/`textures` fence
 * so partial JSON does not flash into the chat while a reply is still streaming.
 */
export function stripGalleryBlocks(text: string): string {
  if (!text) return text;
  let out = text.replace(MODELS_BLOCK_RE, "").replace(TEXTURES_BLOCK_RE, "");
  // Drop a trailing, not-yet-closed gallery fence (streaming in progress).
  const openModels = out.lastIndexOf("```models");
  const openTextures = out.lastIndexOf("```textures");
  const open = Math.max(openModels, openTextures);
  if (open !== -1 && out.indexOf("```", open + 3) === -1) {
    out = out.slice(0, open);
  }
  return out.replace(/\n{3,}/g, "\n\n").trim();
}
