import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { EventEmitter } from "node:events";
const require = createRequire(new URL("../webchat/package.json", import.meta.url));
const { register } = await import(pathToFileURL(require.resolve("tsx/esm/api")).href);
register();
const { config } = await import("../webchat/server/src/config.ts");
config.mode = "local";
config.storageAccountName = "teststorage";
config.mediaControlSecret = "test-only-secret-".repeat(3);
const { WorkspaceArchive, workspaceOwner, workspaceTitle, readWorkspaceDeletionAck, registerWorkspaceRoutes, registerWorkspace, ownerFromCookie, getWorkspaceArchive } = await import("../webchat/server/src/workspaces.ts");
const { scopeFor, signBrowserKey, signWorkspaceDeletion, signEnvelope, COOKIE_NAME, MEDIA_PREFIX } = await import("../webchat/server/src/mediaSecurity.ts");
const { stripWorkspaceTitleBlocks } = await import("../webchat/client/src/lib/parseMarkdown.ts");

class MemoryStore {
  values = new Map();
  removed = [];
  beforeWrite;
  failRemove = false;
  async read(name) { return this.values.has(name) ? structuredClone(this.values.get(name)) : null; }
  async write(name, value, etag) {
    if (this.beforeWrite) { const hook = this.beforeWrite; this.beforeWrite = undefined; await hook(name, value); }
    const previous = this.values.get(name);
    if (previous && !etag || etag && previous?.etag !== etag) throw { statusCode: 412 };
    this.values.set(name, { value: structuredClone(value), etag: String(Number(previous?.etag ?? 0) + 1) });
  }
  async url(name) { return `https://teststorage.blob.core.windows.net/screenshots/${name}?test=true`; }
  async list(prefix) { return [...this.values.entries()].filter(([name]) => name.startsWith(prefix)).map(([name, entry]) => ({ name, etag: entry.etag, leased: entry.leased })); }
  async remove(name, etag) {
    if (this.failRemove) { this.failRemove = false; throw new Error("Fake storage outage."); }
    const entry = this.values.get(name);
    if (entry && entry.etag !== etag) throw { statusCode: 412 };
    this.removed.push(name); this.values.delete(name);
  }
}
const key = "1".repeat(64);
const owner = workspaceOwner(key);
const other = workspaceOwner("2".repeat(64));
const id = "12345678-1234-4123-8123-123456789abc";
const otherId = "22345678-1234-4123-8123-123456789abc";
const scope = scopeFor(key, id);
const job = "e".repeat(32);
const path = `webchat-workspaces/${owner}/${id}.json`;
const marker = `workspace-scenes/${scope}/deleted.json`;
const titleBlock = (title) => `\n\`\`\`workspacetitle\n${JSON.stringify({ title })}\n\`\`\``;
const turn = (text, turnId = "turn-1") => ({ id: turnId, user: "Create a scene", assistant: text, status: "done", created_at: 1 });
const store = new MemoryStore();
const archive = new WorkspaceArchive(store);
await archive.claim(owner, id);
await archive.update(owner, id, (workspace) => {
  workspace.media_scope = scope;
  workspace.binding = { agentSessionId: "session-persisted", agentVersion: "1", foundryConversationId: "conv_persisted" };
});
await archive.turn(owner, id, turn("Done" + titleBlock("Le voyage des cubes")), "resp_1");
assert.equal((await archive.get(owner, id)).title, "Le voyage des cubes");
assert.equal((await archive.get(owner, id)).turns[0].assistant, "Done");
assert.equal(workspaceTitle(titleBlock(" ")), undefined);
assert.equal(workspaceTitle(titleBlock("x".repeat(91))), undefined);
assert.equal(stripWorkspaceTitleBlocks("Done" + titleBlock("Secret metadata")), "Done");
assert.equal(stripWorkspaceTitleBlocks('Done\n```workspacetitle\n{"tit'), "Done");
await archive.rename(owner, id, " Mon titre choisi ");
await archive.turn(owner, id, turn("Done" + titleBlock("Do not overwrite"), "turn-2"), "resp_2");
assert.equal((await archive.get(owner, id)).title, "Mon titre choisi");
assert.equal((await archive.get(owner, id)).title_source, "manual");
await assert.rejects(archive.rename(owner, id, ""), /title/);
await assert.rejects(archive.rename(other, id, "Hijack"), /not available/);

// A rename winning the ETag race must survive the retry of an automatic title.
await archive.update(owner, id, (workspace) => { delete workspace.title_source; });
store.beforeWrite = async (name) => {
  if (name !== path) return;
  const entry = store.values.get(path);
  entry.value.title = "Concurrent manual title";
  entry.value.title_source = "manual";
  entry.etag = String(Number(entry.etag) + 1);
};
await archive.turn(owner, id, turn("Done" + titleBlock("Late automatic title"), "turn-race"), "resp_race");
assert.equal((await archive.get(owner, id)).title, "Concurrent manual title");
const cleanupCalls = [];
const cleanup = {
  sandbox: async (workspaceId, binding) => { cleanupCalls.push(["sandbox", workspaceId, binding.agentSessionId]); },
  conversation: async (binding) => { cleanupCalls.push(["conversation", binding.foundryConversationId]); },
};
for (const confirm of [undefined, false, "true", 1]) await assert.rejects(archive.delete(owner, id, scope, confirm, cleanup), /confirm/);
await assert.rejects(archive.delete(other, id, scope, true, cleanup), /not available/);
await assert.rejects(archive.delete(owner, id, "f".repeat(32), true, cleanup), /scope/);
const release = await archive.beginOperation(owner, id);
await assert.rejects(archive.delete(owner, id, scope, true, cleanup), /still active/);
await release();

const manifest = `video-jobs/${scope}/${job}.json`;
await archive.job(owner, id, { id: job });
for (const state of ["queued", "rendering", "encoding", "wavespeed_uploading", "wavespeed_submitting", "wavespeed_processing", "submission_unknown"]) {
  const previous = await store.read(manifest);
  await store.write(manifest, { id: job, scope, state }, previous?.etag);
  await assert.rejects(archive.delete(owner, id, scope, true, cleanup), /job prevents deletion/);
  assert.equal((await store.read(path)).value.deleted_at, undefined);
}
let previous = await store.read(manifest);
await store.write(manifest, { id: job, scope, state: "cancelled", prediction_id: "paid-still-running" }, previous.etag);
await assert.rejects(archive.delete(owner, id, scope, true, cleanup), /job prevents deletion/);
previous = await store.read(manifest);
await store.write(manifest, { id: job, scope, state: "completed" }, previous.etag);
await store.write(`${manifest}.lock`, {});
store.values.get(`${manifest}.lock`).leased = true;
await assert.rejects(archive.delete(owner, id, scope, true, cleanup), /worker is still active/);
store.values.get(`${manifest}.lock`).leased = false;
const ownedFile = `workspace-artifacts/${scope}/render_20260101_ab12.png`;
const sharedFile = `workspace-artifacts/${scope}/shared.png`;
const video = `videos/${scope}/${job}/preview.mp4`;
const audio = `music-audio/${scope}/${"a".repeat(32)}.wav`;
const reference = `references/${scope}/${"b".repeat(32)}.png`;
const scene = `workspace-scenes/${scope}/scene.blend`;
const foreign = `workspace-artifacts/${"f".repeat(32)}/foreign.png`;
const legacy = "screenshot_20260101_ab12.png";
for (const name of [ownedFile, sharedFile, video, audio, reference, scene, foreign, legacy]) await store.write(name, { fake_file: true });
await archive.claim(other, otherId);
await archive.update(other, otherId, (workspace) => { workspace.media_scope = scopeFor("2".repeat(64), otherId); });
await archive.turn(other, otherId, turn(`![shared](https://teststorage.blob.core.windows.net/screenshots/${sharedFile})`), "resp_other");
assert.equal(cleanupCalls.length, 0);
store.failRemove = true;
await assert.rejects(archive.delete(owner, id, scope, true, cleanup), /cleanup failed/);
const tombstone = (await store.read(path)).value;
assert.equal(tombstone.turns, undefined);
assert.equal(tombstone.deleted_at > 0, true);
assert.equal(tombstone.deletion.sandbox_pending, false);
assert.equal(tombstone.deletion.conversation_pending, false);
assert.equal(tombstone.deletion.pending_blobs.length > 0, true);
assert.equal((await archive.list(owner))[0].deletion_pending, true);
assert.equal((await archive.list(owner))[0].thumbnail_url, undefined);
await assert.rejects(archive.update(owner, id, () => {}), /permanently deleted/);
await assert.rejects(archive.turn(owner, id, turn("Late callback"), null), /permanently deleted/);
await assert.rejects(archive.job(owner, id, { id: job }), /permanently deleted/);
await assert.rejects(archive.rename(owner, id, "Resurrect"), /permanently deleted/);
const restarted = new WorkspaceArchive(store);
await restarted.delete(owner, id, scope, true, cleanup);
await restarted.delete(owner, id, scope, true, cleanup);
assert.equal(cleanupCalls.length, 2);
assert.equal((await store.read(path)).value.deletion.complete, true);
assert.equal((await store.read(path)).value.deletion.binding.agentSessionId, undefined);
assert.equal((await restarted.list(owner)).length, 0);
assert.equal((await restarted.list(other)).length, 1);
for (const name of [ownedFile, video, audio, reference, scene, manifest, `${manifest}.lock`]) assert.equal(store.values.has(name), false, name);
for (const name of [sharedFile, foreign, legacy, marker]) assert.equal(store.values.has(name), true, name);
assert.equal(store.removed.every((name) => name.includes(`/${scope}/`) && name !== marker), true);

const ackResponse = (ack) => Response.json({ output: [{ content: [{ type: "output_text", text: JSON.stringify(ack) }] }], status: "completed" });
await readWorkspaceDeletionAck(ackResponse({ workspace_deleted: id }), id);
await assert.rejects(readWorkspaceDeletionAck(ackResponse({ workspace_deleted: otherId }), id), /acknowledgment/);
await assert.rejects(readWorkspaceDeletionAck(ackResponse({ workspace_deleted: id, extra: true }), id), /acknowledgment/);
const signedDelete = signWorkspaceDeletion(config.mediaControlSecret, scope, id);
assert.deepEqual(JSON.parse(Buffer.from(signedDelete.slice(MEDIA_PREFIX.length).split(".")[0], "base64url").toString()), { scope, text: "", workspace_delete: id });

// Mutations are exercised through a memory-only Express server, never real records.
const express = require("express");
const app = express();
app.use(express.json());
const routeStore = new MemoryStore();
const routeArchive = new WorkspaceArchive(routeStore);
await routeArchive.claim(owner, id);
await routeArchive.update(owner, id, (workspace) => { workspace.media_scope = scope; });
registerWorkspaceRoutes(app, undefined, cleanup, routeArchive);
const server = app.listen(0, "127.0.0.1");
await new Promise((resolve) => server.once("listening", resolve));
try {
  const base = `http://127.0.0.1:${server.address().port}/api/workspaces/${id}`;
  const cookie = `${COOKIE_NAME}=${signBrowserKey(key, config.mediaControlSecret)}`;
  const mutate = (suffix, body, headers = {}) => fetch(`${base}/${suffix}`, {
    method: "POST", headers: { "Content-Type": "application/json", Origin: config.clientOrigin, Cookie: cookie, ...headers }, body: JSON.stringify(body),
  });
  assert.equal((await mutate("rename", { title: "Browser title" })).status, 200);
  assert.equal((await mutate("rename", { title: "Spoof" }, { Cookie: "" })).status, 403);
  assert.equal((await mutate("delete", { confirm: true }, { Origin: "https://evil.example" })).status, 403);
  assert.equal((await mutate("delete", { confirm: "true" })).status, 409);
  assert.equal((await mutate("delete", { confirm: true }, { Cookie: `${COOKIE_NAME}=${signBrowserKey("2".repeat(64), config.mediaControlSecret)}` })).status, 409);
  assert.equal((await mutate("delete", { confirm: true })).status, 200);
  assert.equal((await mutate("delete", { confirm: true })).status, 200);
  assert.equal((await mutate("rename", { title: "Late rename" })).status, 400);
} finally { await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())); }

// Persisted Foundry IDs are cleaned even after cache loss; all endpoints are fake.
const originalFetch = globalThis.fetch;
const { DefaultAzureCredential } = require("@azure/identity");
const { DefaultAzureCredential: ESMDefaultAzureCredential } = await import("../webchat/node_modules/@azure/identity/dist/esm/index.js");
const credentials = [...new Set([DefaultAzureCredential, ESMDefaultAzureCredential])];
const originalTokens = credentials.map((credential) => credential.prototype.getToken);
credentials.forEach((credential) => { credential.prototype.getToken = async () => ({ token: "fake-test-token", expiresOnTimestamp: Date.now() + 3600_000 }); });
config.mode = "foundry";
config.foundryAgentBase = "https://fake.invalid/agents/test";
const calls = [];
let items = [{ id: "msg_test" }];
globalThis.fetch = async (url, options = {}) => {
  calls.push([String(url), options.method ?? "GET", options.headers]);
  if (String(url).includes("/items?")) return Response.json({ data: items });
  if (String(url).includes("/items/msg_test")) items = [];
  return new Response(null, { status: 204 });
};
try {
  const { deleteWorkspaceSandbox, deleteWorkspaceConversation, getSessionInfo } = await import("../webchat/server/src/sessions.ts");
  assert.equal(getSessionInfo(id), null);
  await deleteWorkspaceSandbox(id, { agentSessionId: "persisted-session" });
  await deleteWorkspaceConversation({ foundryConversationId: "conv_persisted" });
  assert.equal(calls[0][0].includes("/sessions/persisted-session"), true);
  assert.equal(calls[0][2]["x-session-isolation-key"], id);
  assert.equal(calls.some(([url, method]) => url.includes("/items/msg_test") && method === "DELETE"), true);
  globalThis.fetch = async () => new Response("fake error", { status: 500 });
  await assert.rejects(deleteWorkspaceSandbox(id, { agentSessionId: "persisted-session" }), /cleanup failed/);
} finally {
  globalThis.fetch = originalFetch;
  credentials.forEach((credential, index) => { credential.prototype.getToken = originalTokens[index]; });
  config.mode = "local";
}

// Register the real relay against a Blob adapter backed only by this memory store.
const { BlobServiceClient } = require("@azure/storage-blob");
const { BlobServiceClient: ESMBlobServiceClient } = await import("../webchat/node_modules/@azure/storage-blob/dist/esm/index.js");
const blobClients = [...new Set([BlobServiceClient, ESMBlobServiceClient])];
const originalContainers = blobClients.map((client) => client.prototype.getContainerClient);
const relayStore = new MemoryStore();
const memoryContainer = () => ({
  getProperties: async () => ({}),
  getBlockBlobClient: (name) => ({
    getProperties: async () => {
      const entry = await relayStore.read(name);
      if (!entry) throw { statusCode: 404 };
      return { etag: entry.etag, contentLength: Buffer.byteLength(JSON.stringify(entry.value)) };
    },
    downloadToBuffer: async () => Buffer.from(JSON.stringify((await relayStore.read(name)).value)),
    uploadData: async (data, options) => relayStore.write(name, JSON.parse(data.toString()), options.conditions.ifMatch),
  }),
  getBlobClient: (name) => ({ deleteIfExists: async (options) => relayStore.remove(name, options.conditions.ifMatch) }),
  listBlobsFlat: async function* ({ prefix }) {
    for (const entry of await relayStore.list(prefix)) yield { name: entry.name, properties: { etag: entry.etag } };
  },
});
blobClients.forEach((client) => { client.prototype.getContainerClient = memoryContainer; });
class FakeVoiceSocket extends EventEmitter {
  readyState = 1;
  sent = [];
  send(data, options, callback) {
    this.sent.push(Buffer.isBuffer(data) ? data : Buffer.from(data));
    (typeof options === "function" ? options : callback)?.();
  }
  close() { if (this.readyState === 3) return; this.readyState = 3; this.emit("close"); }
  terminate() { this.close(); }
}
const waitFor = async (predicate, label) => {
  const deadline = Date.now() + 3000;
  while (!await predicate()) {
    if (Date.now() >= deadline) throw new Error(`Timed out waiting for ${label}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
};
const emitFrame = (socket, value) => socket.emit("message", Buffer.from(JSON.stringify(value)), false);
let finishHistory;
let finishArchive;
let historyRequested = false;
config.mode = "foundry";
globalThis.fetch = async (url, options) => {
  assert.match(String(url), /fake\.invalid\/agents\/test\/endpoint\/protocols\/openai\/conversations\/conv_registered_voice\/items/);
  assert.equal(options.method, "POST");
  const items = JSON.parse(options.body).items;
  assert.equal(items[0].content, "Create a registered voice scene.");
  historyRequested = true;
  await new Promise((resolve) => { finishHistory = resolve; });
  return new Response(null, { status: 200 });
};
try {
  const { relay } = await import("../webchat/server/src/voice.ts");
  const cookie = `${COOKIE_NAME}=${signBrowserKey(key, config.mediaControlSecret)}`;
  const relayOwner = ownerFromCookie(cookie);
  const liveArchive = getWorkspaceArchive();
  let counter = 3;
  const setup = async () => {
    const workspaceId = `${counter++}2345678-1234-4123-8123-123456789abc`;
    await registerWorkspace(relayOwner, workspaceId);
    const browser = new FakeVoiceSocket();
    const upstream = new FakeVoiceSocket();
    const mediaScope = scopeFor(key, workspaceId);
    relay(browser, upstream, undefined, "conv_registered_voice", signEnvelope(config.mediaControlSecret, mediaScope, "Voice request follows."), workspaceId);
    const record = () => liveArchive.get(relayOwner, workspaceId);
    const blocked = () => assert.rejects(liveArchive.delete(relayOwner, workspaceId, mediaScope, true, cleanup), /still active/);
    assert.equal((await record()).active_operations?.length ?? 0, 0, "prewarm must not mark capture busy");
    return { browser, upstream, workspaceId, mediaScope, record, blocked };
  };
  const done = await setup();
  emitFrame(done.browser, { type: "start" });
  await waitFor(async () => (await done.record()).active_operations?.length === 1, "registered capture operation");
  await done.blocked();
  done.browser.emit("message", Buffer.from([0, 0, 0, 0]), true);
  emitFrame(done.upstream, { type: "stt", text: "Create a registered voice scene.", final: true });
  await waitFor(() => done.browser.sent.some((frame) => frame.toString().includes('"type":"stt"')), "transcript forwarding");
  emitFrame(done.upstream, { type: "delta", text: "The scene is ready.", delta: "The scene is ready." });
  await waitFor(() => done.browser.sent.some((frame) => frame.toString().includes('"type":"delta"')), "assistant forwarding");
  emitFrame(done.upstream, { type: "done", text: "The scene is ready.", history_commit_required: true });
  await waitFor(() => historyRequested, "Foundry history commit");
  await done.blocked();
  assert.equal((await done.record()).active_operations.length, 1, "history commit must retain deletion protection");
  emitFrame(done.browser, { type: "cancel" });
  await done.blocked();
  done.browser.close();
  await done.blocked();
  assert.equal((await done.record()).active_operations.length, 1, "cancel/close cannot release a pending history commit");
  let archiveRequested = false;
  const blockArchive = async (name, value) => {
    if (name !== `webchat-workspaces/${relayOwner}/${done.workspaceId}.json` || value.turns?.length !== 1) {
      relayStore.beforeWrite = blockArchive;
      return;
    }
    archiveRequested = true;
    await new Promise((resolve) => { finishArchive = resolve; });
  };
  relayStore.beforeWrite = blockArchive;
  finishHistory();
  await waitFor(() => archiveRequested, "durable turn archive");
  await done.blocked();
  assert.equal((await done.record()).turns.length, 0, "archive gate must hold the turn write");
  assert.equal((await done.record()).active_operations.length, 1, "archive completion must retain deletion protection");
  finishArchive();
  await waitFor(async () => (await done.record()).active_operations?.length === 0, "done/archive operation release");
  assert.equal((await done.record()).turns.length, 1, "done must archive before release");
  assert.equal((await done.record()).turns[0].user, "Create a registered voice scene.");
  assert.equal((await done.record()).turns[0].assistant, "The scene is ready.");
  await liveArchive.delete(relayOwner, done.workspaceId, done.mediaScope, true, cleanup);
  done.browser.close(); done.upstream.close();

  for (const ending of ["cancel", "close"]) {
    const capture = await setup();
    emitFrame(capture.browser, { type: "start" });
    await waitFor(async () => (await capture.record()).active_operations?.length === 1, `${ending} capture operation`);
    await capture.blocked();
    if (ending === "cancel") emitFrame(capture.browser, { type: "cancel" });
    else capture.browser.close();
    await waitFor(async () => (await capture.record()).active_operations?.length === 0, `${ending} release`);
    await liveArchive.delete(relayOwner, capture.workspaceId, capture.mediaScope, true, cleanup);
    capture.browser.close(); capture.upstream.close();
  }
  const prewarm = await setup();
  await liveArchive.delete(relayOwner, prewarm.workspaceId, prewarm.mediaScope, true, cleanup);
  prewarm.browser.close(); prewarm.upstream.close();
} finally {
  finishHistory?.();
  finishArchive?.();
  blobClients.forEach((client, index) => { client.prototype.getContainerClient = originalContainers[index]; });
  globalThis.fetch = originalFetch;
  config.mode = "local";
}
console.log("PASS registered relay: prewarm stays idle, capture/history/archive block deletion, done/cancel/close release durable operations");
console.log("PASS model-title metadata/manual rename races, verified ownership/confirmation, safe exact scoped cleanup/shared-file preservation, active and ambiguous job rejection, durable retry tombstones, local acknowledgments and fake Foundry deletion");
