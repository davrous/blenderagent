// Smoke test for W3C trace propagation in the webchat voice relay.
// Run after building the server: node devTools/test_voice_relay.mjs

import assert from "node:assert/strict";
import { buildTraceHeaders } from "../webchat/server/dist/voice.js";

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

console.log("voice relay W3C trace headers: OK");
