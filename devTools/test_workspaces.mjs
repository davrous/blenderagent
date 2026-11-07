import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
const require = createRequire(new URL("../webchat/package.json", import.meta.url));
const { register } = await import(pathToFileURL(require.resolve("tsx/esm/api")).href);
register();
const { config } = await import("../webchat/server/src/config.ts");
config.storageAccountName = "teststorage";
const { WorkspaceArchive, TurnArchiveCollector } = await import("../webchat/server/src/workspaces.ts");

class MemoryStore {
  values = new Map();
  async read(key) { return this.values.has(key) ? structuredClone(this.values.get(key)) : null; }
  async write(key, value, etag) {
    const old = this.values.get(key);
    if ((old && !etag) || (etag && old?.etag !== etag)) throw { statusCode: 412 };
    this.values.set(key, { value: structuredClone(value), etag: String(Number(old?.etag ?? 0) + 1) });
  }
  async url(blob) { return `https://teststorage.blob.core.windows.net/screenshots/${blob}?refreshed=true`; }
}
const storage = new MemoryStore();
const archive = new WorkspaceArchive(storage);
const owner = "a".repeat(64);
const other = "b".repeat(64);
const id = "12345678-1234-4123-8123-123456789abc";
await archive.claim(owner, id);
await assert.rejects(archive.claim(other, id), /not available/);
await archive.update(owner, id, (workspace) => { workspace.binding = { agentSessionId: "session-1234", agentVersion: "1", foundryConversationId: "conv_1234" }; });
const image = "https://teststorage.blob.core.windows.net/screenshots/screenshot_1.png?expired=true";
const render = "https://teststorage.blob.core.windows.net/screenshots/render_final.png?expired=true";
await archive.turn(owner, id, { id: "turn-1", user: "Create my first scene", assistant: `![viewport](${image})`, status: "done", created_at: 1 }, "resp_1", true);
await archive.audio(owner, id, "https://teststorage.blob.core.windows.net/screenshots/music-audio/one.wav?old=true");
await archive.job(owner, id, { id: "e".repeat(32), viewport_url: image, poster_url: render,
  output_url: "https://teststorage.blob.core.windows.net/screenshots/videos/one.mp4?expired=true" });
const restarted = new WorkspaceArchive(storage);
const detail = await restarted.detail(owner, id);
assert.equal(detail.binding.agentSessionId, "session-1234");
assert.equal(detail.binding.foundryConversationId, "conv_1234");
assert.match(detail.turns[0].assistant, /refreshed=true/);
assert.equal(detail.resources.filter((resource) => resource.kind === "audio").length, 1);
assert.equal(detail.resources.filter((resource) => resource.kind === "video").length, 1);
assert.equal(detail.scene_expected, true);
assert.equal((await restarted.list(other)).length, 0);
assert.match((await restarted.list(owner))[0].thumbnail_url, /render_final/);
await assert.rejects(restarted.detail(other, id), /not available/);
await archive.turn(owner, id, { id: "turn-1", user: "duplicate", assistant: "duplicate", status: "done", created_at: 2 }, "resp_1");
assert.equal((await archive.get(owner, id)).turns.length, 1);

const collector = new TurnArchiveCollector();
const stream = 'event: response.output_text.delta\r\ndata: {"delta":"Bonjour 🎵"}\r\n\r\nevent: response.completed\r\ndata: {"response":{"id":"resp_final"}}\r\n\r\n';
const bytes = new TextEncoder().encode(stream);
for (let offset = 0; offset < bytes.length; offset += 3) collector.feed(bytes.slice(offset, offset + 3));
assert.equal(collector.reply, "Bonjour 🎵");
assert.equal(collector.responseId, "resp_final");
assert.equal(collector.failed, false);
console.log("PASS private browser ownership, durable Foundry bindings, transcript and audio/video archive, render thumbnails, refreshed media URLs and chunked SSE");
