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
