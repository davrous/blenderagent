import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { once } from "node:events";
import { access } from "node:fs/promises";
import { mock } from "node:test";
const webchatRequire = createRequire(new URL("../webchat/package.json", import.meta.url));
const { register } = await import(pathToFileURL(webchatRequire.resolve("tsx/esm/api")).href);
register();
const { loadHealth } = await import("../webchat/client/src/api/health.ts");
const originalHealthFetch = globalThis.fetch;
const healthFixture = { mode: "local", agentUrl: "http://localhost:8088", model: "test", mediaEnabled: true, voiceEnabled: true };
const flushHealth = () => new Promise((resolve) => setImmediate(resolve));
mock.timers.enable({ apis: ["setTimeout"] });
try {
  let attempts = 0;
  const healthResults = [];
  let failures = 0;
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.cache, "no-store");
    attempts++;
    if (attempts === 1) throw new TypeError("fetch failed");
    if (attempts === 2) return Response.json(healthFixture, { status: 503 });
    if (attempts === 3) return new Response("not json");
    if (attempts === 4) return Response.json({ mediaEnabled: true });
    return Response.json(healthFixture);
  };
  const stop = loadHealth((health) => healthResults.push(health), () => failures++);
  await flushHealth();
  assert.equal(failures, 1);
  for (const delay of [1000, 2000, 4000, 5000]) {
    mock.timers.tick(delay - 1);
    await flushHealth();
    assert.equal(healthResults.length, 0);
    mock.timers.tick(1);
    await flushHealth();
  }
  assert.equal(attempts, 5);
  assert.equal(failures, 4);
  assert.deepEqual(healthResults, [healthFixture]);
  mock.timers.tick(30000);
  await flushHealth();
  assert.equal(attempts, 5);
  stop();

  const disabled = { ...healthFixture, mediaEnabled: false, mediaDisabledReason: "Media disabled by configuration" };
  globalThis.fetch = async () => { attempts++; return Response.json(disabled); };
  const stopDisabled = loadHealth((health) => healthResults.push(health), () => assert.fail("Valid disabled configuration must not retry"));
  await flushHealth();
  mock.timers.tick(30000);
  await flushHealth();
  assert.equal(attempts, 6);
  assert.deepEqual(healthResults.at(-1), disabled);
  stopDisabled();

  let resolveHealth;
  let pendingSignal;
  globalThis.fetch = (_url, { signal }) => {
    pendingSignal = signal;
    return new Promise((resolve) => { resolveHealth = resolve; });
  };
  const stopPending = loadHealth(() => assert.fail("Unmounted health check updated state"), () => assert.fail("Unmounted health check retried"));
  stopPending();
  assert.equal(pendingSignal.aborted, true);
  resolveHealth(Response.json(healthFixture));
  await flushHealth();

  let timeoutAttempts = 0;
  let timeouts = 0;
  globalThis.fetch = (_url, { signal }) => {
    timeoutAttempts++;
    return new Promise((_resolve, reject) => signal.addEventListener("abort", () => reject(new Error("aborted")), { once: true }));
  };
  const stopTimeout = loadHealth(() => assert.fail("Stalled health request succeeded"), () => timeouts++);
  mock.timers.tick(5000);
  await flushHealth();
  assert.equal(timeouts, 1);
  stopTimeout();
  mock.timers.tick(30000);
  await flushHealth();
  assert.equal(timeoutAttempts, 1);
} finally {
  globalThis.fetch = originalHealthFetch;
  mock.timers.reset();
}
console.log("PASS startup health retry/backoff, HTTP/JSON failures, recovery, disabled config, timeout and unmount cleanup");
const { MEDIA_PREFIX, signEnvelope, signBrowserKey, verifyBrowserKey, scopeFor, ownedReferences, mediaEnabled, requireOrigin } = await import("../webchat/server/src/mediaSecurity.ts");

const secret = "local-tests-only-secret-32-characters-long";
const key = "a".repeat(64);
const conversation = "12345678-1234-4123-8123-123456789abc";
const scope = scopeFor(key, conversation);
assert.match(scope, /^[0-9a-f]{32}$/);
assert.equal(scope, scopeFor(key, conversation.toUpperCase()));
assert.notEqual(scope, scopeFor("b".repeat(64), conversation));
assert.notEqual(scope, scopeFor(key, "22345678-1234-4123-8123-123456789abc"));
const cookie = signBrowserKey(key, secret);
assert.equal(verifyBrowserKey(cookie, secret), key);
assert.equal(verifyBrowserKey(cookie, "another-secret"), null);
assert.equal(verifyBrowserKey(cookie.replace(/^a/, "b"), secret), null);
assert.equal(verifyBrowserKey("bad", secret), null);
assert.equal(mediaEnabled(""), false);
assert.equal(mediaEnabled("a".repeat(31)), false);
assert.equal(mediaEnabled(secret), true);
const reference = `references/${scope}/${"c".repeat(32)}.png`;
assert.deepEqual(ownedReferences([reference], scope), [reference]);
for (const invalid of [["https://example.com/image.png"], [reference.replace(scope, "d".repeat(32))], Array(5).fill(reference), [reference + "/../x"], "bad"]) {
  assert.throws(() => ownedReferences(invalid, scope));
}
const text = `${MEDIA_PREFIX}untrusted-browser-text.abc`;
const envelope = signEnvelope(secret, scope, text, [reference]);
const [part, signature] = envelope.slice(MEDIA_PREFIX.length).split(".");
assert.equal(signature, createHmac("sha256", secret).update(part).digest("hex"));
assert.deepEqual(JSON.parse(Buffer.from(part, "base64url").toString("utf8")), { scope, text, references: [reference] });
let next = false;
let status;
const response = { status(code) { status = code; return this; }, json() {} };
requireOrigin("http://localhost:5173")({ headers: { origin: "https://evil.example" } }, response, () => { next = true; });
assert.equal(status, 403);
assert.equal(next, false);
requireOrigin("http://localhost:5173")({ headers: { origin: "http://localhost:5173" } }, response, () => { next = true; });
assert.equal(next, true);
console.log("PASS media signatures, cookie tamper checks, scoped references, envelope text safety, Origin checks");

process.env.AGENT_MODE = "local";
process.env.MEDIA_CONTROL_SECRET = secret;
process.env.CLIENT_ORIGIN = "http://localhost:5173";
const { default: express } = await import("../webchat/node_modules/express/index.js");
const { config } = await import("../webchat/server/src/config.ts");
const { registerMediaRoutes, readJobStream, validateJob, withDeadline } = await import("../webchat/server/src/videoJobs.ts");
const deadline = new AbortController();
const timed = withDeadline(new Promise(() => {}), deadline.signal);
deadline.abort();
await assert.rejects(timed, /timed out/);
const { validateMetadata, sniffMedia, MAX_REFERENCE_BYTES } = await import("../webchat/server/src/referenceUploads.ts");
assert.equal(MAX_REFERENCE_BYTES, 200 * 1024 * 1024);
assert.equal(sniffMedia(Buffer.from("not-media")), null);
for (const mime of ["image/png", "image/jpeg", "image/webp"]) {
  validateMetadata({ streams: [{ codec_type: "video", width: 4000, height: 4000 }] }, mime);
  assert.throws(() => validateMetadata({ streams: [{ codec_type: "video", width: 4001, height: 4000 }] }, mime));
}
const videoMetadata = (width, height, duration) => ({ streams: [{ codec_type: "video", width, height }], format: { duration } });
validateMetadata(videoMetadata(1920, 1080, "4"), "video/mp4");
validateMetadata(videoMetadata(1920, 1080, "30"), "video/mp4");
for (const metadata of [videoMetadata(1921, 1080, "10"), videoMetadata(1920, 1081, "10"), videoMetadata(1920, 1080, "3.9"), videoMetadata(1920, 1080, "30.1"), videoMetadata(0, 0, "NaN")]) {
  assert.throws(() => validateMetadata(metadata, "video/mp4"));
}
const jobId = "e".repeat(32);
let liveState = "awaiting_seedance_approval";
const descriptor = () => ({ id: jobId, state: liveState, progress: 0.5, mode: "seedance", duration_seconds: 10, fps: 24, resolution: "720p", seedance_enabled: true });
const sse = (job) => {
  const text = "```videojob\n" + JSON.stringify(job) + "\n```";
  return [...text].map((delta) => `event: response.output_text.delta\r\ndata: ${JSON.stringify({ delta })}\r\n\r\n`).join("");
};
const encoded = new TextEncoder().encode(sse(descriptor()));
const splitStream = new ReadableStream({ start(controller) {
  for (let offset = 0; offset < encoded.length; offset += 7) controller.enqueue(encoded.slice(offset, offset + 7));
  controller.close();
} });
assert.equal((await readJobStream(new Response(splitStream), jobId)).id, jobId);
await assert.rejects(readJobStream(new Response(sse({ ...descriptor(), id: "f".repeat(32) })), jobId));
assert.throws(() => validateJob({ ...descriptor(), state: "invented" }, jobId));
assert.equal(validateJob({ ...descriptor(), preview_url: "https://evil.example/a.mp4" }, jobId).preview_url, undefined);
console.log("PASS media metadata limits, chunked CRLF SSE, descriptor validation");

const upstream = express();
upstream.use(express.json());
const calls = [];
const captured = [];
upstream.post("/responses", (req, res) => {
  captured.push(req.body);
  if (!req.body.input.startsWith(MEDIA_PREFIX)) { res.type("text/event-stream").end('data: {"type":"response.output_text.delta","delta":"text-only"}\n\n'); return; }
  const [payload, signature] = req.body.input.slice(MEDIA_PREFIX.length).split(".");
  assert.equal(signature, createHmac("sha256", secret).update(payload).digest("hex"));
  const decoded = JSON.parse(Buffer.from(payload, "base64url").toString());
  assert.equal(decoded.scope, scope);
  if (!decoded.action) { res.type("text/event-stream").end('data: {"type":"response.output_text.delta","delta":"signed chat"}\n\n'); return; }
  calls.push(decoded.action);
  if (decoded.action.type === "approve") liveState = "wavespeed_processing";
  if (decoded.action.type === "cancel") liveState = "cancelled";
  res.type("text/event-stream").end(sse(descriptor()));
});
const upstreamServer = upstream.listen(0, "127.0.0.1");
await once(upstreamServer, "listening");
const upstreamUrl = `http://127.0.0.1:${upstreamServer.address().port}/responses`;
const app = express();
app.use(express.json());
let uploadPath;
let uploadedName;
registerMediaRoutes(app, async (body, input, control) => {
  assert.equal(control, true);
  assert.deepEqual(body, { conversation_id: conversation });
  return { url: upstreamUrl, headers: { "Content-Type": "application/json" }, payload: { input, store: false, stream: true, agent_session_id: conversation } };
}, async (_account, blobName, file) => {
  await access(file);
  uploadPath = file;
  uploadedName = blobName;
});
const server = app.listen(0, "127.0.0.1");
await once(server, "listening");
const base = `http://127.0.0.1:${server.address().port}`;
const headers = { Cookie: `blender_media_browser=${cookie}`, Origin: config.clientOrigin, "Content-Type": "application/json" };
const route = `/api/video-jobs/${jobId}`;
try {
  let response = await fetch(`${base}${route}?conversation_id=${conversation}`, { headers });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal((await response.json()).state, "awaiting_seedance_approval");
  response = await fetch(`${base}${route}/approve`, { method: "POST", headers: { ...headers, Origin: "https://evil.example" }, body: JSON.stringify({ conversation_id: conversation }) });
  assert.equal(response.status, 403);
  const previousCalls = calls.length;
  response = await fetch(`${base}${route}/approve`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation, resolution: "8k" }) });
  assert.equal(response.status, 400);
  assert.equal(calls.length, previousCalls);
  response = await fetch(`${base}${route}/approve`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation }) });
  assert.equal(response.status, 200);
  assert.deepEqual(calls.slice(-2), [{ type: "status", job_id: jobId }, { type: "approve", job_id: jobId, prompt: "", resolution: "720p", generate_audio: false }]);
  response = await fetch(`${base}${route}/approve`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation }) });
  assert.equal(response.status, 409);
  assert.equal(calls.at(-1).type, "status");
  liveState = "submission_unknown";
  response = await fetch(`${base}${route}/cancel`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation }) });
  assert.equal(response.status, 409);
  const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=", "base64");
  const form = new FormData();
  form.append("file", new Blob([png], { type: "image/png" }), "reference.png");
  response = await fetch(`${base}/api/references?conversation_id=${conversation}`, { method: "POST", headers: { Cookie: headers.Cookie, Origin: headers.Origin }, body: form });
  const result = await response.json();
  assert.equal(response.status, 201, JSON.stringify(result));
  assert.match(result.blob_name, new RegExp(`^references/${scope}/[0-9a-f]{32}\\.png$`));
  assert.equal(result.blob_name, uploadedName);
  await assert.rejects(access(uploadPath));
  for (const invalid of ["magic", "multiple", "field"]) {
    const badForm = new FormData();
    badForm.append("file", new Blob([invalid === "magic" ? "not PNG" : png], { type: "image/png" }), "ref.png");
    if (invalid === "multiple") badForm.append("second", new Blob([png], { type: "image/png" }), "extra.png");
    if (invalid === "field") badForm.append("scope", scope);
    response = await fetch(`${base}/api/references?conversation_id=${conversation}`, { method: "POST", headers: { Cookie: headers.Cookie, Origin: headers.Origin }, body: badForm });
    assert.equal(response.status, 400, invalid);
  }
  config.mediaControlSecret = "";
  response = await fetch(`${base}${route}?conversation_id=${conversation}`, { headers });
  assert.equal(response.status, 503);
  config.mediaControlSecret = secret;
  console.log("PASS offline HTTP polling, live approval guard/defaults, terminal cancellation, upload ownership/cleanup/magic/multipart limits, disabled mode");

  const startProxy = async (mediaSecret) => {
    const reservation = createServer().listen(0, "127.0.0.1");
    await once(reservation, "listening");
    const port = reservation.address().port;
    await new Promise((resolve) => reservation.close(resolve));
    const child = spawn(process.execPath, ["--import", pathToFileURL(webchatRequire.resolve("tsx")).href, fileURLToPath(new URL("../webchat/server/src/index.ts", import.meta.url))], {
      env: { ...process.env, AGENT_MODE: "local", AGENT_LOCAL_URL: upstreamUrl.replace(/\/responses$/, ""), PORT: String(port), VOICE_ENABLED: "false", MEDIA_CONTROL_SECRET: mediaSecret, CLIENT_ORIGIN: config.clientOrigin },
      stdio: ["ignore", "pipe", "pipe"],
    });
    try {
      await new Promise((resolve, reject) => {
        let output = "";
        const timer = setTimeout(() => reject(new Error("Test proxy startup timed out")), 15_000);
        child.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Test proxy exited: ${code}`)); });
        child.stdout.on("data", (chunk) => { output += chunk; if (output.includes("listening on")) { clearTimeout(timer); resolve(); } });
      });
      return { child, url: `http://127.0.0.1:${port}` };
    } catch (error) { child.kill(); throw error; }
  };
  for (const mediaSecret of [secret, ""]) {
    const proxy = await startProxy(mediaSecret);
    try {
      response = await fetch(`${proxy.url}/api/health`, { headers });
      assert.equal((await response.json()).mediaEnabled, !!mediaSecret);
      assert.equal(response.headers.get("set-cookie"), null);
      response = await fetch(`${proxy.url}/api/chat`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation, input: "hello", previous_response_id: "response-prior" }) });
      assert.equal(response.status, 200);
      await response.text();
      assert.equal(captured.at(-1).agent_session_id, conversation);
      assert.equal(captured.at(-1).previous_response_id, "response-prior");
      if (mediaSecret) {
        response = await fetch(`${proxy.url}/api/health`);
        const issuedCookie = response.headers.get("set-cookie");
        assert.match(issuedCookie, /HttpOnly/i);
        assert.match(issuedCookie, /SameSite=Strict/i);
        const untrusted = signEnvelope(secret, "f".repeat(32), "", undefined, { type: "approve", job_id: jobId });
        response = await fetch(`${proxy.url}/api/chat`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation, input: untrusted, scope: "f".repeat(32), action: { type: "approve", job_id: jobId } }) });
        assert.equal(response.status, 200);
        await response.text();
        const wrapped = JSON.parse(Buffer.from(captured.at(-1).input.slice(MEDIA_PREFIX.length).split(".")[0], "base64url").toString());
        assert.equal(wrapped.text, untrusted);
        assert.equal(wrapped.scope, scope);
        assert.equal(wrapped.action, undefined);
        response = await fetch(`${proxy.url}/api/chat`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation, input: "", references: [reference] }) });
        assert.equal(response.status, 200);
        await response.text();
        for (const references of [Array(5).fill(reference), ["references/ffffffffffffffffffffffffffffffff/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.png"], ["https://evil.example/a.png"]]) {
          response = await fetch(`${proxy.url}/api/chat`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation, input: "hello", references }) });
          assert.equal(response.status, 400);
        }
        response = await fetch(`${proxy.url}${route}?conversation_id=${conversation}&previous_response_id=forged`, { headers });
        assert.equal(response.status, 200);
        assert.equal(captured.at(-1).stream, true);
        assert.equal(captured.at(-1).store, false);
        assert.equal(captured.at(-1).agent_session_id, conversation);
        assert.equal("conversation" in captured.at(-1), false);
        assert.equal("previous_response_id" in captured.at(-1), false);
      } else {
        assert.equal(captured.at(-1).input, "hello");
        response = await fetch(`${proxy.url}/api/chat`, { method: "POST", headers, body: JSON.stringify({ conversation_id: conversation, input: MEDIA_PREFIX + "forged" }) });
        assert.equal(response.status, 400);
      }
      response = await fetch(`${proxy.url}/api/reset`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ conversation_id: conversation }) });
      assert.equal(response.status, 403);
    } finally {
      const exited = once(proxy.child, "exit");
      proxy.child.kill();
      await exited;
    }
  }
  console.log("PASS actual proxy: text-only fallback, cookie reuse/flags, safe envelope wrapping, owned references, control affinity/store:false/no history, reset Origin");
} finally {
  server.closeAllConnections();
  upstreamServer.closeAllConnections();
  await Promise.all([new Promise((resolve) => server.close(resolve)), new Promise((resolve) => upstreamServer.close(resolve))]);
}

const memoryStorage = new Map();
globalThis.localStorage = { getItem: (name) => memoryStorage.get(name) ?? null, setItem: (name, value) => memoryStorage.set(name, value) };
globalThis.window = globalThis;
const media = await import("../webchat/client/src/api/media.ts");
const markdown = await import("../webchat/client/src/lib/parseMarkdown.ts");
const voiceSecurity = await import("../webchat/server/src/mediaSecurity.ts");
const voiceSecret = "voice-test-secret-".repeat(3);
const voiceOwner = "a".repeat(64);
const voiceCookie = `${voiceSecurity.COOKIE_NAME}=${voiceSecurity.signBrowserKey(voiceOwner, voiceSecret)}`;
const voiceContext = voiceSecurity.voiceMediaContext(voiceCookie, conversation, voiceSecret);
const voicePayload = JSON.parse(Buffer.from(voiceContext.slice(voiceSecurity.MEDIA_PREFIX.length).split(".")[0], "base64url").toString());
assert.deepEqual(voicePayload, { scope: voiceSecurity.scopeFor(voiceOwner, conversation), text: "Voice request follows." });
assert.equal(voiceSecurity.voiceMediaContext(undefined, conversation, voiceSecret), undefined);
assert.equal(voiceSecurity.voiceMediaContext(voiceCookie + "tampered", conversation, voiceSecret), undefined);
assert.equal(voiceSecurity.voiceMediaContext(voiceCookie, "invalid", voiceSecret), undefined);
assert.equal(voiceSecurity.voiceMediaContext(voiceCookie, conversation, "short"), undefined);
console.log("PASS voice media context: verified browser ownership, same typed scope, no actions, fail closed for invalid inputs");
const fence = "```videojob\n" + JSON.stringify({ ...descriptor(), action: "approve" }) + "\n```";
assert.deepEqual(markdown.extractVideoJobIds(fence + fence), [jobId]);
assert.deepEqual(markdown.extractVideoJobIds('```videojob\n{"id":"../../evil"}\n```'), []);
assert.equal(markdown.stripVideoJobBlocks("Hello\n```videojob\n{\"id\":"), "Hello");
assert.equal(markdown.isVideoLink("https://test.blob.core.windows.net/screenshots/a.mp4?sig=test"), true);
const timelineMessage = (id, role, text, status = "done") => ({ id, role, text, status, rawBuffer: text, currentStatus: null });
const timelineKeys = (timeline) => timeline.entries.map((entry) => entry.kind === "message" ? `message:${entry.message.id}` : `video:${entry.id}`);
const secondJobId = "f".repeat(32);
const secondFence = "```videojob\n" + JSON.stringify({ id: secondJobId }) + "\n```";
const renderTurn = [timelineMessage("request", "user", "Render the scene"), timelineMessage("render", "assistant", fence)];
const firstTimeline = markdown.buildChatTimeline(renderTurn);
assert.deepEqual(timelineKeys(firstTimeline), ["message:request", "message:render", `video:${jobId}`]);
const nextTurn = [...renderTurn, timelineMessage("iterate", "user", "Make the cube blue"), timelineMessage("reply", "assistant", "Updated the scene")];
assert.deepEqual(timelineKeys(markdown.buildChatTimeline(nextTurn)), [...timelineKeys(firstTimeline), "message:iterate", "message:reply"]);
const repeated = markdown.buildChatTimeline([...nextTurn, timelineMessage("second-render", "assistant", fence + secondFence + secondFence)]);
assert.deepEqual(timelineKeys(repeated), [...timelineKeys(firstTimeline), "message:iterate", "message:reply", "message:second-render", `video:${secondJobId}`]);
assert.deepEqual(repeated.jobIds, [jobId, secondJobId]);
const partial = timelineMessage("partial", "assistant", '```videojob\n{"id":"' + jobId + '"}', "streaming");
assert.deepEqual(markdown.buildChatTimeline([partial]).jobIds, []);
assert.deepEqual(markdown.buildChatTimeline([{ ...partial, text: partial.text + "\n```" }]).jobIds, [jobId]);
assert.deepEqual(markdown.buildChatTimeline([timelineMessage("quoted", "user", fence)]).jobIds, []);
assert.deepEqual(markdown.buildChatTimeline([timelineMessage("invalid", "assistant", '```videojob\n{"id":"bad"}\n```')]).jobIds, []);
const recovered = markdown.buildChatTimeline(nextTurn, [jobId, jobId, "invalid"]);
assert.deepEqual(timelineKeys(recovered), [`video:${jobId}`, "message:request", "message:render", "message:iterate", "message:reply"]);
assert.deepEqual(markdown.buildChatTimeline([], [jobId]).jobIds, [jobId]);
const manyIds = Array.from({ length: 101 }, (_, index) => index.toString(16).padStart(32, "0"));
const capped = markdown.buildChatTimeline([timelineMessage("kept", "user", "Hello")], manyIds);
assert.deepEqual(capped.jobIds, manyIds.slice(1));
assert.equal(capped.entries.length, 101);
assert.equal(capped.entries.at(-1).message.id, "kept");
assert.deepEqual(markdown.buildChatTimeline([], media.loadJobIds("other-conversation")), { entries: [], jobIds: [] });
console.log("PASS chronological job placement, later turns, first occurrence dedup, streaming fences, recovered jobs and retention");
media.saveJobIds(conversation, [jobId, "invalid"]);
assert.deepEqual(media.loadJobIds(conversation), [jobId]);
assert.deepEqual(media.loadJobIds("other-conversation"), []);
assert.equal(media.paidEstimate(10, 10, "720p"), 4.4);
assert.equal(media.paidEstimate(10, 10, "4k"), 22);
assert.equal(media.TERMINAL_JOB_STATES.has("submission_unknown"), true);
const originalFetch = globalThis.fetch;
let resolveFetch;
let fetchCount = 0;
globalThis.fetch = () => { fetchCount++; return new Promise((resolve) => { resolveFetch = resolve; }); };
try {
  const first = media.requestVideoJob(conversation, jobId);
  const second = media.requestVideoJob(conversation, jobId);
  assert.equal(first, second);
  assert.equal(fetchCount, 1);
  await assert.rejects(media.requestVideoJob(conversation, jobId, "approve"), /already in progress/);
  resolveFetch(new Response(JSON.stringify(descriptor())));
  await first;
} finally { globalThis.fetch = originalFetch; }
console.log("PASS client ID-only parsing, partial fence hiding, MP4 recognition, durable scoped IDs, paid estimates and single-flight requests");