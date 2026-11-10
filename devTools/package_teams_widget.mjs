import { mkdir, readdir, copyFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
if (!process.argv[2]) throw new Error("Usage: node devTools\\package_teams_widget.mjs <empty-output-directory>");
const output = path.resolve(process.argv[2]);
await mkdir(output, { recursive: true });
if ((await readdir(output)).length) throw new Error("The deployment output directory must be empty.");

const renderer = process.env.TEAMS_WIDGET_RENDERER_ORIGIN?.trim() ?? "";
if (!/^https:\/\/[a-f0-9]{32}\.widget-renderer\.usercontent\.microsoft$/.test(renderer)) {
  throw new Error("TEAMS_WIDGET_RENDERER_ORIGIN must be the exact Teams widget renderer HTTPS origin.");
}
for (const name of ["widget.js", "widget.css", "studio_small_09_1k.hdr"]) {
  const destination = path.join(output, "teams-widget", "v1", name);
  await mkdir(path.dirname(destination), { recursive: true });
  await copyFile(path.join(root, "webchat", "client", "dist", "teams-widget", "v1", name), destination);
}
await writeFile(path.join(output, "staticwebapp.config.json"), JSON.stringify({
  routes: [
    { route: "/health", rewrite: "/health.json" },
    { route: "/teams-widget/v1/studio_small_09_1k.hdr", headers: { "Cache-Control": "no-store" } },
    { route: "/teams-widget/*", headers: { "Cache-Control": "no-cache, must-revalidate" } },
  ],
  globalHeaders: {
    "Access-Control-Allow-Origin": renderer,
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    "Access-Control-Allow-Headers": "Range",
    "Access-Control-Expose-Headers": "Content-Length, Content-Type, ETag",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
  },
  mimeTypes: { ".hdr": "application/octet-stream" },
}, null, 2) + "\n");
await writeFile(path.join(output, "health.json"), '{"status":"ok"}\n');
await writeFile(path.join(output, "index.html"),
  "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>Blender Teams widget assets</title>" +
  "<h1>Blender Teams widget assets</h1><p>Open Blender Agent in Teams to view an exported scene. " +
  "This site hosts public viewer assets only, not scenes or chat APIs.</p></html>\n");
console.log(`Static Web Apps assets packaged at ${output}. No server, chat UI, credentials or scenes are included.`);
