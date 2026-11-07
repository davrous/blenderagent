import { create } from "zustand";
import { streamChat } from "../api/stream";
import { voice, type VoiceStatus } from "../api/voice";
import type { ReferenceUpload } from "../api/media";
import { isMusicConfirmation, type MicMode, type MusicCandidate, type MusicRequest, type MusicGenerationOptions } from "../api/music";
import { createWorkspace, loadWorkspace, type WorkspaceDetail, type WorkspaceBinding } from "../api/workspaces";
import { saveJobIds } from "../api/media";

export type Role = "user" | "assistant";
export type Status = "streaming" | "done" | "error";

export interface Message {
  id: string;
  role: Role;
  text: string;
  rawBuffer: string;
  currentStatus: string | null;
  status: Status;
  errorText?: string;
  referenceNames?: string[];
  audioUrl?: string;
}

interface ChatState {
  workspaceView: "gallery" | "chat" | "resources";
  workspaceLoading: boolean;
  workspaceError: string | null;
  workspaceBinding: WorkspaceBinding | null;
  replayHistory: boolean;
  showGallery: () => void;
  showResources: () => void;
  showChat: () => void;
  startWorkspace: () => Promise<void>;
  resumeWorkspace: (id: string) => Promise<void>;
  setWorkspaceError: (message: string | null) => void;
  voiceInputAudio: (url: string) => void;
  messages: Message[];
  previousResponseId: string | null;
  conversationId: string;
  isStreaming: boolean;
  abortController: AbortController | null;
  // Voice turn state (shares the same message list / scene as typed turns).
  voiceStatus: VoiceStatus;
  voiceActive: boolean;
  voiceHint: string | null;
  micMode: MicMode;
  musicCandidate: MusicCandidate | null;
  musicOptions: MusicGenerationOptions;
  setMusicOptions: (options: MusicGenerationOptions) => void;
  setMicMode: (mode: MicMode) => void;
  setMusicCandidate: (candidate: MusicCandidate | null) => void;
  confirmMusicScene: () => Promise<void>;
  declineMusicScene: () => Promise<void>;
  send: (input: string, references?: ReferenceUpload[]) => Promise<void>;
  selectModel: (modelUrl: string, name: string) => Promise<void>;
  selectTexture: (assetId: string, name: string) => Promise<void>;
  reset: () => void;
  setVoiceStatus: (s: VoiceStatus) => void;
  voiceBeginTurn: (transcript: string) => void;
  voiceAppendDelta: (delta: string) => void;
  voiceFinalizeTurn: (responseId: string | null) => void;
  voiceFail: (message: string) => void;
}

const CONVERSATION_ID_STORAGE_KEY = "webchat.conversationId";
const TURN_PROGRESS_INTERVAL_MS = 30_000;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function generateConversationId(): string {
  const c = (globalThis as any).crypto as Crypto | undefined;
  if (c && typeof c.randomUUID === "function") {
    return c.randomUUID();
  }
  // Fallback: RFC4122 v4 from getRandomValues.
  const b = new Uint8Array(16);
  c!.getRandomValues(b);
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = Array.from(b, (x) => x.toString(16).padStart(2, "0"));
  return `${h.slice(0, 4).join("")}-${h.slice(4, 6).join("")}-${h.slice(6, 8).join("")}-${h.slice(8, 10).join("")}-${h.slice(10, 16).join("")}`;
}

/**
 * Read the conversation id from localStorage, or generate+persist a new one.
 * Persisting across reloads is required so the saved Blender scene blob
 * (keyed on this UUID) can be reloaded after a tab refresh.
 */
function loadOrCreateConversationId(): string {
  try {
    const stored = globalThis.localStorage?.getItem(CONVERSATION_ID_STORAGE_KEY);
    if (stored && UUID_RE.test(stored)) return stored;
  } catch {
    // localStorage may be unavailable (private mode, SSR) — fall through.
  }
  const fresh = generateConversationId();
  try {
    globalThis.localStorage?.setItem(CONVERSATION_ID_STORAGE_KEY, fresh);
  } catch {
    // ignore — conversation will not survive reload, but turn-to-turn still works.
  }
  return fresh;
}

function rotateConversationId(): string {
  const fresh = generateConversationId();
  try {
    globalThis.localStorage?.setItem(CONVERSATION_ID_STORAGE_KEY, fresh);
  } catch {
    // ignore
  }
  return fresh;
}

// Matches a complete italic status block surrounded by blank lines:
// "\n\n*…some text…*\n\n" — used by ToolStatusMiddleware.
// We require both delimiters so we never extract partial deltas.
const STATUS_BLOCK_RE = /\n{2}\*([^*\n]+)\*\n{2}/;

/**
 * Append a streaming delta to a message buffer, extracting any complete
 * `*status*` blocks into `currentStatus` (shown as a pill). Shared by the typed
 * and voice paths so both render identically.
 */
function applyDelta(
  rawBuffer: string,
  currentStatus: string | null,
  delta: string,
): { rawBuffer: string; text: string; currentStatus: string | null } {
  let raw = rawBuffer + delta;
  let status = currentStatus;
  let match: RegExpMatchArray | null;
  while ((match = raw.match(STATUS_BLOCK_RE)) !== null) {
    status = match[1].trim();
    const start = match.index!;
    const end = start + match[0].length;
    // Replace the block with a single blank line so paragraphs stay separated.
    raw = raw.slice(0, start) + "\n\n" + raw.slice(end);
  }
  return { rawBuffer: raw, text: raw, currentStatus: status };
}

let idCounter = 0;
const newId = () => `m${Date.now()}-${++idCounter}`;

// Id of the assistant message currently being filled by a voice turn.
let voiceAssistantId: string | null = null;

// Detects the "orphaned tool call" failure: a prior turn was interrupted after
// the model asked for a tool but before the tool output was recorded, so the
// server-side history is corrupted and every replay fails with a 400. Retrying
// can NEVER succeed — the session must be reset. Matches both the raw upstream
// error and the agent's friendly wording.
const CORRUPT_HISTORY_RE =
  /no tool output found for function call|got out of sync/i;

function isCorruptedHistoryError(payload: any): boolean {
  const candidates = [
    payload?.error?.message,
    payload?.response?.error?.message,
    payload?.message,
    typeof payload === "string" ? payload : undefined,
  ].filter((s): s is string => typeof s === "string");
  const haystack = candidates.length ? candidates.join(" ") : JSON.stringify(payload ?? "");
  return CORRUPT_HISTORY_RE.test(haystack);
}

export const useChatStore = create<ChatState>((set, get) => {
  const activateWorkspace = (workspace: WorkspaceDetail, fresh: boolean): void => {
    voice.dispose();
    const messages: Message[] = workspace.turns.flatMap((turn) => [
      { id: `${turn.id}-user`, role: "user" as const, text: turn.user, rawBuffer: turn.user,
        currentStatus: null, status: "done" as const, ...(turn.audio_url ? { audioUrl: turn.audio_url } : {}) },
      { id: `${turn.id}-assistant`, role: "assistant" as const,
        ...applyDelta("", null, turn.assistant), currentStatus: null, status: turn.status,
        ...(turn.status === "error" ? { errorText: "Tour interrompu ou échoué lors de cette réalisation." } : {}) },
    ]);
    voiceAssistantId = null;
    let storageWarning: string | null = null;
    try {
      localStorage.setItem(CONVERSATION_ID_STORAGE_KEY, workspace.id);
      saveJobIds(workspace.id, workspace.job_ids);
    } catch {
      storageWarning = "Le stockage du navigateur est indisponible. La réalisation reste dans le catalogue serveur.";
    }
    set({
      conversationId: workspace.id, messages, previousResponseId: null,
      workspaceBinding: workspace.binding, workspaceView: workspace.binding.expired ? "resources" : "chat",
      workspaceError: storageWarning ?? workspace.recovery_note ?? null, replayHistory: !fresh, voiceStatus: "idle", voiceActive: false, voiceHint: null,
      micMode: "voice", musicCandidate: null, musicOptions: { duration_seconds: 15, render_mode: "standard" },
    });
  };
  // Recover from a corrupted server-side conversation history (orphaned tool
  // call). We DROP the poisoned response chain (`previousResponseId`) but KEEP
  // the conversation id so the per-conversation Blender scene is preserved. In
  // foundry mode `/api/reset` deletes the broken session; the next turn lazily
  // creates a fresh one bound to the same conversation id.
  const recoverCorruptedSession = async (): Promise<void> => {
    const convId = get().conversationId;
    set({ previousResponseId: null });
    try {
      await fetch("/api/reset", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: convId }),
        keepalive: true,
      });
    } catch {
      // Best-effort: clearing previousResponseId already breaks the retry loop.
    }
  };

  // Shared implementation for a normal agent turn — used by typed input and by
  // silent gallery-selection notes. `userBubbleText` is what shows in the user
  // bubble (null hides it entirely); `agentInput` is what is sent to the agent.
  const runAgentTurn = async (
    agentInput: string,
    userBubbleText: string | null,
    references: ReferenceUpload[] = [],
    music?: MusicRequest,
  ): Promise<void> => {
    if (get().workspaceBinding?.expired) {
      set({ workspaceError: "Ce sandbox a expiré. Les ressources restent consultables, mais la scène ne peut pas être remplacée silencieusement." });
      return;
    }
    if (get().isStreaming || get().voiceActive || ["connecting", "listening", "thinking", "analyzing"].includes(get().voiceStatus)) return;
    const trimmedInput = agentInput.trim();
    if (!trimmedInput && !references.length) return;

    const newMessages: Message[] = [];
    if (userBubbleText !== null) {
      newMessages.push({
        id: newId(),
        role: "user",
        text: userBubbleText,
        referenceNames: references.map((reference) => reference.name),
        rawBuffer: userBubbleText,
        currentStatus: null,
        status: "done",
      });
    }
    const assistantId = newId();
    newMessages.push({
      id: assistantId,
      role: "assistant",
      text: "",
      rawBuffer: "",
      currentStatus: null,
      status: "streaming",
    });

    const ac = new AbortController();
    set({
      messages: [...get().messages, ...newMessages],
      isStreaming: true,
      abortController: ac,
    });

    const turnStartedAt = Date.now();
    const progressTimer = window.setInterval(() => {
      const elapsedSeconds = Math.max(1, Math.floor((Date.now() - turnStartedAt) / 1000));
      const elapsed = elapsedSeconds < 60
        ? `${elapsedSeconds}s`
        : `${Math.floor(elapsedSeconds / 60)}m ${elapsedSeconds % 60}s`;
      set((state) => ({
        messages: state.messages.map((message) =>
          message.id === assistantId && message.status === "streaming"
            ? { ...message, currentStatus: `Still building this complex scene (${elapsed} elapsed)...` }
            : message,
        ),
      }));
    }, TURN_PROGRESS_INTERVAL_MS);

    const appendDelta = (delta: string) => {
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantId
            ? { ...m, ...applyDelta(m.rawBuffer, m.currentStatus, delta) }
            : m,
        ),
      }));
    };

    let finalized = false;
    const finalize = (status: Status, errorText?: string) => {
      // Guard against a trailing `finalize("done")` overwriting a terminal
      // error/state that an in-band SSE `error` frame already set.
      if (finalized) return;
      finalized = true;
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantId
            ? { ...m, status, errorText, currentStatus: status === "done" ? null : m.currentStatus }
            : m,
        ),
        isStreaming: false,
        abortController: null,
      }));
    };

    try {
      await streamChat(
        trimmedInput,
        get().previousResponseId,
        get().conversationId,
        (e) => {
          if (!e.data) return;
          let payload: any;
          try {
            payload = JSON.parse(e.data);
          } catch {
            return;
          }
          const eventType = e.event || payload?.type;

          if (eventType === "response.created" && payload?.response?.id) {
            set({ previousResponseId: payload.response.id });
          } else if (
            eventType === "response.output_text.delta" &&
            typeof payload?.delta === "string"
          ) {
            appendDelta(payload.delta);
          } else if (eventType === "response.completed") {
            const id = payload?.response?.id;
            if (id) set({ previousResponseId: id });
            set({ replayHistory: false });
          } else if (eventType === "archive.warning" && typeof payload?.message === "string") {
            set({ workspaceError: payload.message });
          } else if (eventType === "error" || eventType === "response.failed") {
            if (isCorruptedHistoryError(payload)) {
              // Poisoned session: an orphaned tool call in the server-side
              // history. Retrying replays it forever, so abandon the response
              // chain and reset the session while keeping the scene.
              finalize(
                "error",
                "This conversation got out of sync (a tool call was interrupted " +
                  "before it finished), so the model rejected it. I've started a " +
                  "fresh session — please resend your last message. Your scene is preserved.",
              );
              void recoverCorruptedSession();
            } else {
              finalize("error", payload?.error?.message || payload?.message || JSON.stringify(payload));
            }
          }
        },
        ac.signal,
        references.map((reference) => reference.blob_name),
        music,
        get().replayHistory,
      );
      finalize("done");
    } catch (err) {
      if ((err as any)?.name === "AbortError") {
        finalize("done");
        return;
      }
      finalize("error", err instanceof Error ? err.message : String(err));
    } finally {
      window.clearInterval(progressTimer);
    }
  };

  return {
  workspaceView: "gallery",
  workspaceLoading: false,
  workspaceError: null,
  workspaceBinding: null,
  replayHistory: false,
  setWorkspaceError: (message) => set({ workspaceError: message }),
  showGallery: () => {
    if (get().isStreaming || get().voiceActive || get().workspaceLoading) return;
    voice.dispose();
    set({ workspaceView: "gallery" });
  },
  showResources: () => set({ workspaceView: "resources" }),
  showChat: () => set({ workspaceView: "chat" }),
  startWorkspace: async () => {
    if (get().workspaceLoading || get().isStreaming || get().voiceActive) return;
    set({ workspaceLoading: true, workspaceError: null });
    try {
      const workspace = await createWorkspace();
      activateWorkspace(workspace, true);
    } catch (error) {
      set({ workspaceError: error instanceof Error ? error.message : "Impossible de créer une scène." });
    } finally { set({ workspaceLoading: false }); }
  },
  resumeWorkspace: async (id) => {
    if (get().workspaceLoading || get().isStreaming || get().voiceActive) return;
    set({ workspaceLoading: true, workspaceError: null });
    try {
      activateWorkspace(await loadWorkspace(id), false);
    } catch (error) {
      set({ workspaceError: error instanceof Error ? error.message : "Impossible de reprendre ce travail." });
    } finally { set({ workspaceLoading: false }); }
  },
  messages: [],
  previousResponseId: null,
  conversationId: loadOrCreateConversationId(),
  isStreaming: false,
  abortController: null,
  voiceStatus: "idle",
  voiceActive: false,
  voiceHint: null,
  micMode: "voice",
  musicCandidate: null,
  musicOptions: { duration_seconds: 15, render_mode: "standard" },

  setMusicOptions: (options) => {
    if (get().isStreaming || get().voiceActive || ["connecting", "listening", "thinking", "analyzing"].includes(get().voiceStatus)) return;
    set({ musicOptions: options });
  },

  setMicMode: (mode) => {
    if (get().isStreaming || get().voiceActive || ["connecting", "listening", "thinking", "analyzing"].includes(get().voiceStatus)) return;
    set({ micMode: mode, musicCandidate: null, voiceHint: null });
  },
  setMusicCandidate: (candidate) => {
    if (get().micMode === "music") set({ musicCandidate: candidate?.film ? candidate : null });
  },
  confirmMusicScene: async () => {
    const candidate = get().musicCandidate;
    if (!candidate || get().isStreaming || get().voiceActive) return;
    set({ musicCandidate: null });
    await runAgentTurn("Oui, crée la scène Devfest.", "Oui, crée la scène Devfest.", [], { candidate, action: "generate", ...get().musicOptions });
    if (get().messages.at(-1)?.status === "error") set({ musicCandidate: candidate });
  },
  declineMusicScene: async () => {
    const candidate = get().musicCandidate;
    if (!candidate || get().isStreaming || get().voiceActive) return;
    set({ musicCandidate: null });
    await runAgentTurn("Non, pas de scène.", "Non, pas de scène.", [], { candidate, action: "discuss", ...get().musicOptions });
  },

  reset: () => {
    const { abortController, conversationId } = get();
    abortController?.abort();
    // Best-effort: tell the proxy to drop any foundry session bound to this id.
    // Fire-and-forget; ignore errors and local-mode no-op.
    fetch("/api/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversation_id: conversationId }),
      keepalive: true,
    }).catch(() => {});
    voiceAssistantId = null;
    set({
      messages: [],
      previousResponseId: null,
      conversationId: rotateConversationId(),
      isStreaming: false,
      abortController: null,
      voiceStatus: "idle",
      voiceActive: false,
      voiceHint: null,
      micMode: "voice",
      musicCandidate: null,
      musicOptions: { duration_seconds: 15, render_mode: "standard" },
    });
  },

  setVoiceStatus: (s) => set({ voiceStatus: s, ...(s === "connecting" ? { voiceHint: null } : {}) }),
  voiceInputAudio: (url) => {
    const assistant = get().messages.findIndex((message) => message.id === voiceAssistantId);
    if (assistant < 1) return;
    const userId = get().messages[assistant - 1].id;
    set((state) => ({ messages: state.messages.map((message) => message.id === userId ? { ...message, audioUrl: url } : message) }));
  },

  voiceBeginTurn: (transcript) => {
    const userMsg: Message = {
      id: newId(),
      role: "user",
      text: transcript,
      rawBuffer: transcript,
      currentStatus: null,
      status: "done",
    };
    const assistantId = newId();
    voiceAssistantId = assistantId;
    const assistantMsg: Message = {
      id: assistantId,
      role: "assistant",
      text: "",
      rawBuffer: "",
      currentStatus: null,
      status: "streaming",
    };
    set({
      messages: [...get().messages, userMsg, assistantMsg],
      voiceActive: true,
      voiceHint: null,
      ...(get().micMode === "music" ? { musicCandidate: null } : {}),
    });
  },

  voiceAppendDelta: (delta) => {
    const id = voiceAssistantId;
    if (!id) return;
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === id ? { ...m, ...applyDelta(m.rawBuffer, m.currentStatus, delta) } : m,
      ),
    }));
  },

  voiceFinalizeTurn: (responseId) => {
    const id = voiceAssistantId;
    voiceAssistantId = null;
    set((state) => ({
      messages: state.messages.map((m) =>
        m.id === id ? { ...m, status: "done", currentStatus: null } : m,
      ),
      voiceActive: false,
      replayHistory: false,
      previousResponseId: responseId ?? state.previousResponseId,
    }));
  },

  voiceFail: (message) => {
    const id = voiceAssistantId;
    if (id) {
      voiceAssistantId = null;
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === id ? { ...m, status: "error", errorText: message } : m,
        ),
        voiceActive: false,
        musicCandidate: null,
      }));
    } else {
      set({ voiceHint: message, voiceActive: false, musicCandidate: null });
    }
  },

  send: async (input, references = []) => {
    const candidate = get().micMode === "music" ? get().musicCandidate : null;
    if (candidate && !references.length) {
      const generate = isMusicConfirmation(input);
      if (generate || /^non\b|^no\b/i.test(input.trim())) set({ musicCandidate: null });
      await runAgentTurn(input, input.trim(), [], { candidate, action: generate ? "generate" : "discuss", ...get().musicOptions });
      if (generate && get().messages.at(-1)?.status === "error") set({ musicCandidate: candidate });
    } else {
      await runAgentTurn(input, input.trim(), references);
    }
  },

  // Clicking a model thumbnail in a gallery: send a silent note asking the agent
  // to import that GLB into the Blender scene, then screenshot. Shown to the user
  // as a compact "Selected model …" bubble.
  selectModel: async (modelUrl, name) => {
    const note =
      `[gallery selection] The user picked the 3D model "${name}" ` +
      `(modelUrl: ${modelUrl}) from the model gallery. Import it into the scene ` +
      `by calling download_model with that modelUrl and a short descriptive name, ` +
      `then take a single viewport screenshot so the user sees the result. ` +
      `Do NOT list models again.`;
    await runAgentTurn(note, `Selected model “${name}”`);
  },

  // Clicking a texture thumbnail: send a silent note asking the agent to apply
  // that Poly Haven texture to the most relevant object (or ask which one).
  selectTexture: async (assetId, name) => {
    const note =
      `[gallery selection] The user picked the Poly Haven texture "${name}" ` +
      `(assetId: ${assetId}) from the texture gallery. Apply it with apply_texture ` +
      `to the most relevant existing object in the scene; if it is ambiguous which ` +
      `object they mean, ask which object to texture before applying. Then take a ` +
      `single viewport screenshot. Do NOT list textures again.`;
    await runAgentTurn(note, `Selected texture “${name}”`);
  },
  };
});
