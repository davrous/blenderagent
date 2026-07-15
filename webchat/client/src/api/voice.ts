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

export type VoiceStatus =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking"
  | "error";

export interface VoiceHandlers {
  onStatus: (status: VoiceStatus) => void;
  onUserTranscript: (text: string) => void;
  onAgentDelta: (text: string) => void;
  onAgentDone: (responseId: string | null) => void;
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
  private _status: VoiceStatus = "idle";

  // Capture
  private mediaStream: MediaStream | null = null;
  private captureCtx: AudioContext | null = null;
  private sourceNode: MediaStreamAudioSourceNode | null = null;
  private processor: ScriptProcessorNode | null = null;
  private sinkNode: GainNode | null = null;
  private capturing = false;

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
    if (this.ws && this.ws.readyState === WebSocket.OPEN) return this.ws;
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) {
      await this.waitForOpen(this.ws);
      return this.ws;
    }
    const ws = new WebSocket(this.wsUrl());
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (ev) => this.onMessage(ev);
    ws.onerror = () => {
      this.handlers?.onError("Voice connection error.");
      this.setStatus("error");
    };
    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
    };
    await this.waitForOpen(ws);
    return ws;
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
      };
      ws.addEventListener("open", onOpen);
      ws.addEventListener("error", onErr);
    });
  }

  private sendControl(obj: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
    }
  }

  // ── Capture (push-to-talk) ───────────────────────────────────────────────
  async startListening(): Promise<void> {
    if (!this.handlers) return;
    if (this.capturing) return;

    // Barge-in: interrupt any current playback and tell the agent to stop.
    if (this._status === "speaking") {
      this.stopPlayback();
      this.sendControl({ type: "cancel" });
    }

    this.setStatus("connecting");
    try {
      await this.ensureSocket();
    } catch {
      this.handlers.onError("Could not connect to the voice service.");
      this.setStatus("error");
      return;
    }

    // Unlock the playback AudioContext now, while we still have the user gesture,
    // so scheduled TTS audio is not blocked by the browser autoplay policy.
    this.ensurePlaybackCtx();

    try {
      this.mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      });
    } catch {
      this.handlers.onError("Microphone access was denied.");
      this.setStatus("error");
      return;
    }

    const AC = window.AudioContext ?? (window as any).webkitAudioContext;
    this.captureCtx = new AC();
    await this.captureCtx.resume().catch(() => {});
    const inRate = this.captureCtx.sampleRate;

    this.sourceNode = this.captureCtx.createMediaStreamSource(this.mediaStream);
    this.processor = this.captureCtx.createScriptProcessor(CAPTURE_BUFFER_SIZE, 1, 1);
    this.sinkNode = this.captureCtx.createGain();
    this.sinkNode.gain.value = 0; // don't echo the mic to the speakers

    this.processor.onaudioprocess = (e) => {
      if (!this.capturing) return;
      const input = e.inputBuffer.getChannelData(0);
      const pcm = downsampleToPcm16(input, inRate, TARGET_SAMPLE_RATE);
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(pcm.buffer);
      }
    };

    this.sourceNode.connect(this.processor);
    this.processor.connect(this.sinkNode);
    this.sinkNode.connect(this.captureCtx.destination);

    this.capturing = true;
    this.sendControl({
      type: "start",
      conversation_id: this.handlers.getConversationId(),
    });
    this.setStatus("listening");
  }

  async stopAndCommit(): Promise<void> {
    if (!this.capturing) return;
    this.teardownCapture();
    this.sendControl({
      type: "commit",
      conversation_id: this.handlers?.getConversationId(),
      previous_response_id: this.handlers?.getPreviousResponseId() ?? undefined,
    });
    this.setStatus("thinking");
  }

  private teardownCapture(): void {
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
        } else if (frame.final) {
          h.onError("Didn't catch that — try again.");
          this.setStatus("idle");
        }
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
        h.onAgentDone(frame.response_id ?? null);
        if (!this.capturing) this.setStatus("idle");
        break;
      case "error":
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
