// Smoke test for W3C trace propagation in the webchat voice relay.
// Run after building the server: node devTools/test_voice_relay.mjs

import assert from "node:assert/strict";
import {
  buildFoundryVoiceWsUrl,
  buildTraceHeaders,
  buildVoiceHistoryItems,
  injectSession,
  requiresVoiceHistoryCommit,
} from "../webchat/server/dist/voice.js";
import { runSingleFlight } from "../webchat/server/dist/sessions.js";

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

console.log("voice relay trace, history commit, and single-flight caches: OK");
