/**
 * Browser voice controller for the Blender Scene Agent.
 *
 * Push-to-talk: the caller starts capture on pointer-down and commits on
 * pointer-up. Microphone audio is downsampled to 24 kHz / 16-bit / mono PCM and
 * streamed over `/api/voice` (relayed to the agent's `invocations_ws`). The
 * agent streams back:
 *   - text control frames (`stt`, `delta`, `progress`, `speaking_start`,
 *     `speaking_end`, `done`, `error`, `listening`), and
 *   - binary 24 kHz PCM frames which are scheduled for playback via Web Audio.
 *
 * Scene continuity is client-owned: the `commit` frame carries the current
 * `conversation_id` and `previous_response_id` so voice and text share one
 * server-side Blender scene.
 */

import { isMusicCandidate, type MicMode, type MusicCandidate } from "./music";

export type VoiceStatus =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "analyzing"
  | "speaking"
  | "error";

export interface VoiceHandlers {
  onStatus: (status: VoiceStatus) => void;
  onUserTranscript: (text: string) => void;
  onAgentDelta: (text: string) => void;
  onAgentDone: (responseId: string | null) => void;
  onMusicResult: (candidate: MusicCandidate) => void;
  onInputAudio?: (url: string) => void;
  onArchiveWarning?: (message: string) => void;
  onError: (message: string) => void;
  getConversationId: () => string;
  getPreviousResponseId: () => string | null;
}

const TARGET_SAMPLE_RATE = 24000;
const CAPTURE_BUFFER_SIZE = 4096;

function downsampleToPcm16(
  input: Float32Array,
  inRate: number,
  outRate: number,
): Int16Array {
  if (outRate === inRate) {
    const out = new Int16Array(input.length);
    for (let i = 0; i < input.length; i++) {
      const s = Math.max(-1, Math.min(1, input[i]));
      out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return out;
  }
  const ratio = inRate / outRate;
  const newLength = Math.round(input.length / ratio);
  const out = new Int16Array(newLength);
  let pos = 0;
  for (let i = 0; i < newLength; i++) {
    const idx = i * ratio;
    const i0 = Math.floor(idx);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = idx - i0;
    const sample = input[i0] * (1 - frac) + input[i1] * frac;
    const s = Math.max(-1, Math.min(1, sample));
    out[pos++] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

class VoiceController {
  private handlers: VoiceHandlers | null = null;
  private ws: WebSocket | null = null;
  // Conversation id the current socket was opened for; used to drop a stale
  // socket after a Reset (new conversation) so we don't reuse the old session.
  private wsConversationId: string | null = null;
  // True while opening the socket as a background warm-up (see prewarm()); used
  // to keep connection errors silent until the user actually presses the mic.
  private prewarming = false;
  private _status: VoiceStatus = "idle";

  // Capture
  private mediaStream: MediaStream | null = null;
  private captureCtx: AudioContext | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private sinkNode: GainNode | null = null;
  private capturing = false;
  private captureMode: MicMode = "voice";
  private captureToken = 0;
  private captureSamples = 0;
  private captureTimer: ReturnType<typeof setTimeout> | null = null;

  // Playback
  private playbackCtx: AudioContext | null = null;
  private nextPlayTime = 0;
  private activeSources: AudioBufferSourceNode[] = [];

  isSupported(): boolean {
    const AC = window.AudioContext ?? (window as any).webkitAudioContext;
    return (
      typeof AC === "function" &&
      typeof navigator !== "undefined" &&
      !!navigator.mediaDevices?.getUserMedia &&
      typeof WebSocket === "function"
    );
  }

  init(handlers: VoiceHandlers): void {
    this.handlers = handlers;
  }

  get status(): VoiceStatus {
    return this._status;
  }

  get listening(): boolean {
    return this.capturing;
  }

  private setStatus(s: VoiceStatus): void {
    this._status = s;
    this.handlers?.onStatus(s);
  }

  // ── WebSocket ──────────────────────────────────────────────────────────
  private wsUrl(): string {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const cid = this.handlers?.getConversationId() ?? "";
    return `${proto}//${location.host}/api/voice?sessionId=${encodeURIComponent(cid)}`;
  }

  private async ensureSocket(): Promise<WebSocket> {
    const cid = this.handlers?.getConversationId() ?? "";
    // If the conversation changed (e.g. Reset rotated the id), the existing
    // socket is bound to the old Foundry session — drop it and reconnect.
    if (this.ws && this.wsConversationId !== cid) {
      try {
        this.ws.close(1000, "conversation changed");
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return this.ws;
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
      await this.waitForOpen(this.ws);
      return this.ws;
    }
    const ws = new WebSocket(this.wsUrl());
    this.wsConversationId = cid;
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onerror = () => {
      // Stay silent for background warm-up failures; the first real mic press
      // will retry and surface a connection error itself if it still fails.
      if (this.prewarming) return;
      this.handlers?.onError("Voice connection error.");
      this.setStatus("error");
    };
    ws.onclose = (event) => {
      if (this.ws !== ws) return;
      this.ws = null;
      const interrupted =
        this.capturing ||
        this._status === "connecting" ||
        this._status === "listening" ||
        this._status === "thinking" ||
        this._status === "analyzing" ||
        this._status === "speaking";
      if (!interrupted) return;
      this.captureToken++;
      this.teardownCapture();
      this.stopPlayback();
      if (!this.prewarming) {
        const detail = event.code === 1001
          ? "The voice session expired. Press the microphone to reconnect."
          : "The voice connection closed. Press the microphone to reconnect.";
        this.handlers?.onError(detail);
        this.setStatus("error");
      }
    };
    await this.waitForOpen(ws);
    return ws;
  }

  /**
   * Open the voice WebSocket ahead of the first mic press so the hosted
   * container (Blender + speech pipeline) cold-starts in the background.
   * invocations_ws cold-start on the FIRST connect is expected; without this
   * warm-up the first utterance is lost and the user has to press twice.
   * Best-effort and silent on failure — the first real press retries.
   */
  async prewarm(): Promise<void> {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return;
    this.prewarming = true;
    try {
      await this.ensureSocket();
    } catch {
      /* ignore — the first real press will reconnect */
    } finally {
      this.prewarming = false;
    }
  }

  private waitForOpen(ws: WebSocket): Promise<void> {
    if (ws.readyState === WebSocket.OPEN) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const onOpen = () => {
        cleanup();
        resolve();
      };
      const onErr = () => {
        cleanup();
        reject(new Error("WebSocket failed to open"));
      };
      const cleanup = () => {
        ws.removeEventListener("open", onOpen);
        ws.removeEventListener("error", onErr);
        ws.removeEventListener("close", onErr);
      };
      ws.addEventListener("open", onOpen);
      ws.addEventListener("error", onErr);
      ws.addEventListener("close", onErr);
    });
  }

  private sendControl(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  // ── Capture (push-to-talk) ───────────────────────────────────────────────
  async startListening(mode: MicMode = "voice"): Promise<void> {
    if (!this.handlers) return;
    if (this.capturing) return;
    const token = ++this.captureToken;
    this.captureMode = mode;
    this.captureSamples = 0;
    if (mode === "music") this.stopPlayback();

    // Barge-in: interrupt any current playback and tell the agent to stop.
    if (this._status === "speaking") {
      this.stopPlayback();
      this.sendControl({ type: "cancel" });
    }

    this.setStatus("connecting");
    try {
      await this.ensureSocket();
    } catch {
      if (token !== this.captureToken) return;
      this.handlers.onError("Could not connect to the voice service.");
      this.setStatus("error");
      return;
    }
    if (token !== this.captureToken) return;

    // Unlock the playback AudioContext now, while we still have the user gesture,
    // so scheduled TTS audio is not blocked by the browser autoplay policy.
    this.ensurePlaybackCtx();

    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: mode !== "music",
          noiseSuppression: mode !== "music",
          autoGainControl: mode !== "music",
        },
      });
      if (token !== this.captureToken) {
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
    } catch {
      if (token !== this.captureToken) return;
      this.handlers.onError("Microphone access was denied.");
      this.setStatus("error");
      return;
    }

    const AC = window.AudioContext ?? (window as any).webkitAudioContext;
    const captureCtx: AudioContext = new AC();
    try {
      await captureCtx.resume();
    } catch {
      stream.getTracks().forEach((track) => track.stop());
      void captureCtx.close();
      if (token === this.captureToken) {
        this.handlers.onError("Could not start microphone audio capture.");
        this.setStatus("error");
      }
      return;
    }
    if (token !== this.captureToken) {
      stream.getTracks().forEach((track) => track.stop());
      void captureCtx.close();
      return;
    }
    this.captureCtx = captureCtx;
    this.mediaStream = stream;
    const inRate = this.captureCtx.sampleRate;

    this.sourceNode = this.captureCtx.createMediaStreamSource(this.mediaStream);
    this.processor = this.captureCtx.createScriptProcessor(CAPTURE_BUFFER_SIZE, 1, 1);
    this.sinkNode = this.captureCtx.createGain();
    this.sinkNode.gain.value = 0; // don't echo the mic to the speakers

    this.processor.onaudioprocess = (e) => {
      if (!this.capturing) return;
      const input = e.inputBuffer.getChannelData(0);
      const pcm = downsampleToPcm16(input, inRate, TARGET_SAMPLE_RATE);
      const samples = mode === "music"
        ? pcm.subarray(0, Math.max(0, TARGET_SAMPLE_RATE * 30 - this.captureSamples))
        : pcm;
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        if (samples.length) {
          this.ws.send(samples);
          this.captureSamples += samples.length;
        }
      }
    };

    this.sourceNode.connect(this.processor);
    this.processor.connect(this.sinkNode);
    this.sinkNode.connect(this.captureCtx.destination);

    this.capturing = true;
    this.sendControl({
      type: "start",
      mode,
      conversation_id: this.handlers.getConversationId(),
    });
    this.setStatus("listening");
    if (mode === "music") {
      this.captureTimer = setTimeout(() => void this.stopAndCommit(), 30_000);
    }
  }

  async stopAndCommit(): Promise<void> {
    if (!this.capturing) return;
    // Push-to-talk tail guard: keep capturing for a short window AFTER the
    // button is released so the final word (and a little trailing audio) still
    // reaches STT. Tearing down capture the instant the user stops speaking
    // clips the end of the utterance — the mic delivers audio in ~85 ms blocks
    // and continuous recognition needs a bit of trailing audio to finalize —
    // which is why releasing immediately returns an empty transcript
    // ("Didn't catch that") while holding a beat longer works.
    const TAIL_MS = 350;
    this.setStatus(this.captureMode === "music" ? "analyzing" : "thinking");
    if (this.captureMode === "voice") {
      await new Promise((resolve) => setTimeout(resolve, TAIL_MS));
    }
    if (!this.capturing) return; // cancelled during the tail window
    this.teardownCapture();
    this.sendControl({
      type: "commit",
      mode: this.captureMode,
      conversation_id: this.handlers?.getConversationId(),
      previous_response_id: this.handlers?.getPreviousResponseId() ?? undefined,
    });
  }

  private teardownCapture(): void {
    if (this.captureTimer !== null) clearTimeout(this.captureTimer);
    this.captureTimer = null;
    this.capturing = false;
    try {
      this.processor?.disconnect();
    } catch {
      /* ignore */
    }
    try {
      this.sourceNode?.disconnect();
    } catch {
      /* ignore */
    }
    try {
      this.sinkNode?.disconnect();
    } catch {
      /* ignore */
    }
    if (this.processor) this.processor.onaudioprocess = null;
    this.processor = null;
    this.sourceNode = null;
    this.sinkNode = null;
    if (this.mediaStream) {
      for (const t of this.mediaStream.getTracks()) t.stop();
      this.mediaStream = null;
    }
    if (this.captureCtx) {
      this.captureCtx.close().catch(() => {});
      this.captureCtx = null;
    }
  }

  /** Cancel the current interaction (barge-in / abort) without committing. */
  cancel(): void {
    this.captureToken++;
    if (this.capturing) this.teardownCapture();
    this.stopPlayback();
    this.sendControl({ type: "cancel" });
    this.setStatus("idle");
  }

  // ── Frame routing ─────────────────────────────────────────────────────────
  private onMessage(ev: MessageEvent): void {
    if (ev.data instanceof ArrayBuffer) {
      this.playPcm(ev.data);
      return;
    }
    if (typeof ev.data !== "string") return;
    let frame: any;
    try {
      frame = JSON.parse(ev.data);
    } catch {
      return;
    }
    const h = this.handlers;
    if (!h) return;

    switch (frame.type) {
      case "listening":
        this.setStatus("listening");
        break;
      case "stt":
        if (frame.text && String(frame.text).trim()) {
          h.onUserTranscript(String(frame.text));
          if (frame.mode === "music") this.setStatus("analyzing");
        } else if (frame.final) {
          h.onError("Didn't catch that — try again.");
          this.setStatus("idle");
        }
        break;
      case "music_result":
        if (isMusicCandidate(frame.candidate)) h.onMusicResult(frame.candidate);
        else h.onError("The audio service returned an invalid music result.");
        break;
      case "input_audio":
        if (typeof frame.audio_url === "string") h.onInputAudio?.(frame.audio_url);
        break;
      case "archive.warning":
        if (typeof frame.message === "string") h.onArchiveWarning?.(frame.message);
        break;
      case "delta":
        if (typeof frame.text === "string") h.onAgentDelta(frame.text);
        break;
      case "progress":
        // Voice-only narration; nothing to render in the transcript.
        break;
      case "speaking_start":
        this.setStatus("speaking");
        break;
      case "speaking_end":
        if (!this.capturing && this._status !== "listening") {
          this.setStatus("idle");
        }
        break;
      case "done":
        if (typeof frame.archive_warning === "string") h.onArchiveWarning?.(frame.archive_warning);
        if (frame.mode === "music" && frame.history_commit_failed) {
          h.onError("La musique a été analysée, mais son historique n'a pas pu être enregistré. Réessaie avant de créer la scène.");
          this.setStatus("idle");
          break;
        }
        h.onAgentDone(frame.response_id ?? null);
        if (!this.capturing) this.setStatus("idle");
        break;
      case "error":
        this.captureToken++;
        this.teardownCapture();
        h.onError(String(frame.message ?? "Voice error."));
        this.setStatus("idle");
        break;
      default:
        break;
    }
  }

  // ── Playback ──────────────────────────────────────────────────────────────
  private ensurePlaybackCtx(): AudioContext {
    if (!this.playbackCtx || this.playbackCtx.state === "closed") {
      const AC = window.AudioContext ?? (window as any).webkitAudioContext;
      this.playbackCtx = new AC();
      this.nextPlayTime = 0;
    }
    void this.playbackCtx.resume().catch(() => {});
    return this.playbackCtx;
  }

  private playPcm(buffer: ArrayBuffer): void {
    if (buffer.byteLength === 0) return;
    const ctx = this.ensurePlaybackCtx();
    const int16 = new Int16Array(buffer);
    const float = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) float[i] = int16[i] / 0x8000;

    const audioBuffer = ctx.createBuffer(1, float.length, TARGET_SAMPLE_RATE);
    audioBuffer.getChannelData(0).set(float);

    const src = ctx.createBufferSource();
    src.buffer = audioBuffer;
    src.connect(ctx.destination);

    const startAt = Math.max(ctx.currentTime + 0.02, this.nextPlayTime);
    src.start(startAt);
    this.nextPlayTime = startAt + audioBuffer.duration;

    this.activeSources.push(src);
    src.onended = () => {
      this.activeSources = this.activeSources.filter((s) => s !== src);
    };
  }

  private stopPlayback(): void {
    for (const s of this.activeSources) {
      try {
        s.stop();
      } catch {
        /* ignore */
      }
    }
    this.activeSources = [];
    this.nextPlayTime = 0;
  }

  dispose(): void {
    this.captureToken++;
    this.teardownCapture();
    this.stopPlayback();
    if (this.playbackCtx) {
      this.playbackCtx.close().catch(() => {});
      this.playbackCtx = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* ignore */
      }
      this.ws = null;
    }
    this.setStatus("idle");
  }
}

export const voice = new VoiceController();
