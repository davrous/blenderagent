// Smoke test for W3C trace propagation in the webchat voice relay.
// Run after building the server: node devTools/test_voice_relay.mjs

import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { createRequire } from "node:module";
import {
  buildFoundryVoiceWsUrl,
  buildTraceHeaders,
  buildVoiceHistoryItems,
  injectSession,
  requiresVoiceHistoryCommit,
  relay,
  handleVoiceConnection,
} from "../webchat/server/dist/voice.js";
import { runSingleFlight } from "../webchat/server/dist/sessions.js";
import { config } from "../webchat/server/dist/config.js";

const require = createRequire(new URL("../webchat/package.json", import.meta.url));
const { WebSocketServer } = require("ws");

const generated = buildTraceHeaders({ headers: {} });
assert.equal(generated.traceparent, undefined);

const voiceFrame = Buffer.from(JSON.stringify({ type: "start", media_context: "forged", conversation_id: "browser" }));
const scopedFrame = injectSession(voiceFrame, false, undefined, undefined, "verified-context");
assert.equal(JSON.parse(scopedFrame.data.toString()).media_context, "verified-context");
const unscopedFrame = injectSession(voiceFrame, false, undefined, undefined);
assert.equal(JSON.parse(unscopedFrame.data.toString()).media_context, undefined);
const untypedFrame = Buffer.from(JSON.stringify({ media_context: "forged" }));
assert.equal(JSON.parse(injectSession(untypedFrame, false, undefined, undefined).data.toString()).media_context, undefined);
const hostedFrame = JSON.parse(injectSession(voiceFrame, false, "session-foundry", "conv_shared", "verified-context").data.toString());
assert.equal(hostedFrame.foundry_agent_session_id, "session-foundry");
assert.equal(hostedFrame.foundry_conversation_id, "conv_shared");
assert.equal(hostedFrame.media_context, "verified-context");
assert.equal(injectSession(voiceFrame, true, undefined, undefined, "verified-context").data, voiceFrame);

const parent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01";
const forwarded = buildTraceHeaders({
  headers: {
    traceparent: parent,
    tracestate: "vendor=value",
    baggage: "conversation.kind=voice",
  },
});
assert.equal(forwarded.traceparent, parent);
assert.equal(forwarded.tracestate, "vendor=value");
assert.equal(forwarded.baggage, "conversation.kind=voice");

const unsafe = buildTraceHeaders({ headers: { baggage: "safe=value\r\ninjected=yes" } });
assert.equal(unsafe.baggage, undefined);

const voiceUrl = new URL(
  buildFoundryVoiceWsUrl("session-foundry", "conv_shared"),
  "https://offline.example",
);
assert.equal(voiceUrl.searchParams.get("agent_session_id"), "session-foundry");
assert.equal(voiceUrl.searchParams.get("conversation_id"), "conv_shared");

const historyItems = buildVoiceHistoryItems(" create four cubes ", "Done.");
assert.deepEqual(historyItems[0], {
  type: "message",
  role: "user",
  content: "create four cubes",
});
assert.equal(historyItems[1].role, "assistant");
assert.equal(historyItems[1].status, "completed");
assert.equal(historyItems[1].content[0].type, "output_text");
assert.equal(historyItems[1].content[0].text, "Done.");
assert.equal(
  requiresVoiceHistoryCommit({ type: "done", history_commit_required: true }),
  true,
);
assert.equal(
  requiresVoiceHistoryCommit({ type: "done", history_persisted: false }),
  true,
);
assert.equal(requiresVoiceHistoryCommit({ type: "done" }), false);

const inFlight = new Map();
let createCount = 0;
const concurrent = Array.from({ length: 25 }, () =>
  runSingleFlight(inFlight, "browser-conversation", async () => {
    createCount++;
    await new Promise((resolve) => setTimeout(resolve, 20));
    return "shared-foundry-id";
  }),
);
const ids = await Promise.all(concurrent);
assert.equal(createCount, 1);
assert.deepEqual(new Set(ids), new Set(["shared-foundry-id"]));
assert.equal(inFlight.size, 0);

let failureCount = 0;
await assert.rejects(
  runSingleFlight(inFlight, "retryable", async () => {
    failureCount++;
    throw new Error("temporary failure");
  }),
  /temporary failure/,
);
const retryResult = await runSingleFlight(inFlight, "retryable", async () => {
  failureCount++;
  return "retry-succeeded";
});
assert.equal(retryResult, "retry-succeeded");
assert.equal(failureCount, 2);

class FakeSocket extends EventEmitter {
  readyState = 1;
  sent = [];
  send(data, options) { this.sent.push({ data, isBinary: options.binary }); }
  close() { this.readyState = 3; }
}
const start = { data: Buffer.from('{"type":"start","conversation_id":"browser"}'), isBinary: false };
const audio = { data: Buffer.from([0, 1, 2, 3]), isBinary: true };
const commit = { data: Buffer.from('{"type":"commit"}'), isBinary: false };
for (const alreadyOpen of [false, true]) {
  const browser = new FakeSocket();
  const upstream = new FakeSocket();
  if (!alreadyOpen) upstream.readyState = 0;
  relay(browser, upstream, undefined, undefined, "verified-context", undefined, undefined, [start, audio]);
  browser.emit("message", commit.data, false);
  if (!alreadyOpen) {
    assert.equal(upstream.sent.length, 0);
    upstream.readyState = 1;
    upstream.emit("open");
  }
  upstream.emit("open");
  assert.equal(upstream.sent.length, 3, "startup frames must arrive exactly once");
  assert.equal(JSON.parse(upstream.sent[0].data.toString()).type, "start");
  assert.deepEqual(upstream.sent[1], audio);
  assert.equal(JSON.parse(upstream.sent[2].data.toString()).type, "commit");
  assert.equal(browser.sent.filter((frame) => JSON.parse(frame.data.toString()).type === "relay_ready").length, 1);
}
const stalledBrowser = new FakeSocket();
const stalledUpstream = new FakeSocket();
stalledUpstream.readyState = 0;
relay(stalledBrowser, stalledUpstream);
stalledBrowser.emit("message", Buffer.alloc(4 * 1024 * 1024 + 1), true);
assert.equal(stalledBrowser.readyState, 3);
assert.equal(stalledUpstream.readyState, 3);
assert.match(JSON.parse(stalledBrowser.sent[0].data.toString()).message, /buffer/);

const fixture = new WebSocketServer({ port: 0, host: "127.0.0.1" });
await new Promise((resolve) => fixture.once("listening", resolve));
const oldUrl = config.localVoiceWsUrl;
const oldMode = config.mode;
let watchdog;
try {
  config.mode = "local";
  config.localVoiceWsUrl = `ws://127.0.0.1:${fixture.address().port}/invocations_ws`;
  const received = [];
  const complete = new Promise((resolve, reject) => {
    watchdog = setTimeout(() => reject(new Error("Startup frames did not reach upstream")), 5000);
    fixture.on("connection", (socket) => socket.on("message", (data, isBinary) => {
      received.push({ data, isBinary });
      if (received.length === 3) resolve();
    }));
  });
  const browser = new FakeSocket();
  const setup = handleVoiceConnection(browser, {
    url: "/api/voice?sessionId=98f3b539-3819-4165-8d18-28be46b958ec", headers: {},
  });
  for (const frame of [start, audio, commit]) browser.emit("message", frame.data, frame.isBinary);
  await setup;
  await complete;
  assert.deepEqual(received.map((frame) => frame.isBinary ? "audio" : JSON.parse(frame.data.toString()).type), ["start", "audio", "commit"]);
} finally {
  clearTimeout(watchdog);
  config.localVoiceWsUrl = oldUrl;
  config.mode = oldMode;
  for (const socket of fixture.clients) socket.close(1000);
  await new Promise((resolve) => fixture.close(resolve));
}

console.log("voice relay readiness race, ordered startup frames, buffer limit, trace, history and single-flight caches: OK");
