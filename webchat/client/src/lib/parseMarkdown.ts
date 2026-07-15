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
