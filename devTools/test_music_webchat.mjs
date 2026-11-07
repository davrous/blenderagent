// Offline browser capture, store and confirmation contracts; no Azure calls.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { mock } from "node:test";

const require = createRequire(new URL("../webchat/package.json", import.meta.url));
const { register } = await import(pathToFileURL(require.resolve("tsx/esm/api")).href);
register();
const { parseMusicRequest } = await import("../webchat/server/src/music.ts");
const { signEnvelope, MEDIA_PREFIX } = await import("../webchat/server/src/mediaSecurity.ts");
const { isMusicConfirmation } = await import("../webchat/client/src/api/music.ts");

const candidate = { film: "Original space adventure", track: "Space theme", confidence: "low", evidence: "A fanfare melody." };
const request = { candidate, action: "generate" };
assert.deepEqual(parseMusicRequest(request), request);
for (const invalid of [null, {}, { ...request, action: "approve" }, { ...request, candidate: { ...candidate, film: null } }, { ...request, candidate: { ...candidate, evidence: "" } }]) {
  assert.throws(() => parseMusicRequest(invalid));
}
const envelope = signEnvelope("music-test-secret-".repeat(4), "a".repeat(32), "Oui", undefined, undefined, request);
assert.deepEqual(JSON.parse(Buffer.from(envelope.slice(MEDIA_PREFIX.length).split(".")[0], "base64url")), { scope: "a".repeat(32), text: "Oui", music: request });
for (const text of ["oui", "Oui !", "Oui, vas-y", "yes please"]) assert.equal(isMusicConfirmation(text), true);
for (const text of ["non", "oui mais pas maintenant", "yes don't generate", "quel film ?", ""]) assert.equal(isMusicConfirmation(text), false);

class FakeSocket extends EventTarget {
  static OPEN = 1;
  static CONNECTING = 0;
  static instances = [];
  readyState = 0;
  sent = [];
  constructor() {
    super();
    FakeSocket.instances.push(this);
    queueMicrotask(() => { this.readyState = 1; this.dispatchEvent(new Event("open")); });
  }
  send(value) { this.sent.push(typeof value === "string" ? JSON.parse(value) : value); }
  close() { this.readyState = 3; }
  receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
}

let processor;
let microphoneOptions;
let stoppedTracks = 0;
let permissionRequest = async (options) => {
  microphoneOptions = options;
  return { getTracks: () => [{ stop: () => stoppedTracks++ }] };
};
const node = () => ({ connect() {}, disconnect() {}, gain: { value: 1 } });
class FakeAudioContext {
  sampleRate = 24000;
  state = "running";
  destination = {};
  resume() { return Promise.resolve(); }
  close() { this.state = "closed"; return Promise.resolve(); }
  createMediaStreamSource() { return node(); }
  createGain() { return node(); }
  createScriptProcessor() { processor = node(); return processor; }
}

globalThis.window = { AudioContext: FakeAudioContext, setInterval, clearInterval };
globalThis.WebSocket = FakeSocket;
globalThis.location = { protocol: "http:", host: "localhost" };
Object.defineProperty(globalThis, "navigator", { configurable: true, value: { mediaDevices: { getUserMedia: (options) => permissionRequest(options) } } });
globalThis.localStorage = { getItem: () => null, setItem() {} };
const { voice } = await import("../webchat/client/src/api/voice.ts");
const { useChatStore } = await import("../webchat/client/src/state/chatStore.ts");
voice.init({
  onStatus: (status) => useChatStore.getState().setVoiceStatus(status),
  onUserTranscript: (text) => useChatStore.getState().voiceBeginTurn(text),
  onAgentDelta: (delta) => useChatStore.getState().voiceAppendDelta(delta),
  onAgentDone: (id) => useChatStore.getState().voiceFinalizeTurn(id),
  onMusicResult: (value) => useChatStore.getState().setMusicCandidate(value),
  onError: (error) => useChatStore.getState().voiceFail(error),
  getConversationId: () => useChatStore.getState().conversationId,
  getPreviousResponseId: () => useChatStore.getState().previousResponseId,
});

const flush = () => new Promise((resolve) => setImmediate(resolve));
try {
  useChatStore.getState().setMicMode("music");
  mock.timers.enable({ apis: ["setTimeout"] });
  await voice.startListening("music");
  assert.equal(voice.listening, true);
  assert.deepEqual(microphoneOptions.audio, { channelCount: 1, echoCancellation: false, noiseSuppression: false, autoGainControl: false });
  const socket = FakeSocket.instances.at(-1);
  assert.equal(socket.sent.at(-1).mode, "music");
  for (let index = 0; index < 31; index++) {
    processor.onaudioprocess({ inputBuffer: { getChannelData: () => new Float32Array(24000).fill(0.25) } });
  }
  const totalSamples = socket.sent.filter((value) => value instanceof Int16Array).reduce((sum, value) => sum + value.length, 0);
  assert.equal(totalSamples, 30 * 24000);
  mock.timers.tick(30000);
  await flush();
  assert.equal(socket.sent.at(-1).type, "commit");
  assert.equal(voice.listening, false);
  assert.equal(stoppedTracks, 1);
  socket.receive({ type: "stt", mode: "music", text: "Extrait musical", final: true });
  socket.receive({ type: "delta", text: "\n\n*🎵 J'analyse la musique… 🎶*\n\n" });
  assert.match(useChatStore.getState().messages.at(-1).currentStatus, /🎵/);
  socket.receive({ type: "delta", text: "A film hypothesis. Create the Devfest scene?" });
  socket.receive({ type: "music_result", candidate });
  socket.receive({ type: "done", mode: "music", response_id: null });
  assert.deepEqual(useChatStore.getState().musicCandidate, candidate);
  assert.equal(useChatStore.getState().voiceActive, false);
  useChatStore.getState().voiceBeginTurn("Second musical excerpt");
  socket.receive({ type: "music_result", candidate });
  socket.receive({ type: "done", mode: "music", history_commit_failed: true });
  assert.equal(useChatStore.getState().messages.at(-1).status, "error");
  assert.equal(useChatStore.getState().musicCandidate, null);
  useChatStore.getState().setMusicCandidate(candidate);
  mock.timers.reset();

  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return new Response("event: response.output_text.delta\ndata: {\"delta\":\"Done\"}\n\nevent: response.completed\ndata: {\"response\":{\"id\":\"resp_next\"}}\n\n", { headers: { "Content-Type": "text/event-stream" } });
  };
  try {
    await useChatStore.getState().send("Quel film ?");
    assert.equal(requests.at(-1).music.action, "discuss");
    assert.deepEqual(useChatStore.getState().musicCandidate, candidate);
    await useChatStore.getState().send("oui");
    assert.equal(requests.at(-1).music.action, "generate");
    assert.equal(useChatStore.getState().musicCandidate, null);
    useChatStore.getState().setMusicCandidate(candidate);
    await useChatStore.getState().declineMusicScene();
    assert.equal(requests.at(-1).music.action, "discuss");
    assert.equal(useChatStore.getState().musicCandidate, null);
    useChatStore.getState().setMusicCandidate(candidate);
    useChatStore.getState().setMicMode("voice");
    assert.equal(useChatStore.getState().musicCandidate, null);
    await useChatStore.getState().send("Create a cube");
    assert.equal(requests.at(-1).music, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }

  await voice.startListening();
  assert.deepEqual(microphoneOptions.audio, { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true });
  voice.cancel();
  assert.equal(voice.listening, false);
  assert.equal(FakeSocket.instances.at(-1).sent.at(-1).type, "cancel");
  let resolvePermission;
  permissionRequest = () => new Promise((resolve) => { resolvePermission = resolve; });
  const pending = voice.startListening("music");
  await flush();
  voice.cancel();
  resolvePermission({ getTracks: () => [{ stop: () => stoppedTracks++ }] });
  await pending;
  assert.equal(voice.listening, false);
  assert.equal(voice.status, "idle");
} finally {
  voice.dispose();
  mock.timers.reset();
}
console.log("PASS raw musical capture, exact 30 s cap, cancellation, animated status, hypotheses, confirmation/refusal and normal voice/chat regression");
