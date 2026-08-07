// Smoke test for W3C trace propagation in the webchat voice relay.
// Run after building the server: node devTools/test_voice_relay.mjs

import assert from "node:assert/strict";
import {
  buildFoundryVoiceWsUrl,
  buildTraceHeaders,
  buildVoiceHistoryItems,
  requiresVoiceHistoryCommit,
} from "../webchat/server/dist/voice.js";
import { runSingleFlight } from "../webchat/server/dist/sessions.js";

const generated = buildTraceHeaders({ headers: {} });
assert.equal(generated.traceparent, undefined);

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
