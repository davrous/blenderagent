import { create } from "zustand";
import { streamChat } from "../api/stream";
import type { VoiceStatus } from "../api/voice";

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
}

interface ChatState {
  messages: Message[];
  previousResponseId: string | null;
  conversationId: string;
  isStreaming: boolean;
  abortController: AbortController | null;
  // Voice turn state (shares the same message list / scene as typed turns).
  voiceStatus: VoiceStatus;
  voiceActive: boolean;
  voiceHint: string | null;
  send: (input: string) => Promise<void>;
  reset: () => void;
  setVoiceStatus: (s: VoiceStatus) => void;
  voiceBeginTurn: (transcript: string) => void;
  voiceAppendDelta: (delta: string) => void;
  voiceFinalizeTurn: (responseId: string | null) => void;
  voiceFail: (message: string) => void;
}

const CONVERSATION_ID_STORAGE_KEY = "webchat.conversationId";

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

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  previousResponseId: null,
  conversationId: loadOrCreateConversationId(),
  isStreaming: false,
  abortController: null,
  voiceStatus: "idle",
  voiceActive: false,
  voiceHint: null,

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
    });
  },

  setVoiceStatus: (s) => set({ voiceStatus: s }),

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
      }));
    } else {
      set({ voiceHint: message, voiceActive: false });
    }
  },

  send: async (input) => {
    if (get().isStreaming || get().voiceActive) return;
    const trimmed = input.trim();
    if (!trimmed) return;

    const userMsg: Message = {
      id: newId(),
      role: "user",
      text: trimmed,
      rawBuffer: trimmed,
      currentStatus: null,
      status: "done",
    };
    const assistantId = newId();
    const assistantMsg: Message = {
      id: assistantId,
      role: "assistant",
      text: "",
      rawBuffer: "",
      currentStatus: null,
      status: "streaming",
    };

    const ac = new AbortController();
    set({
      messages: [...get().messages, userMsg, assistantMsg],
      isStreaming: true,
      abortController: ac,
    });

    const appendDelta = (delta: string) => {
      set((state) => ({
        messages: state.messages.map((m) =>
          m.id === assistantId
            ? { ...m, ...applyDelta(m.rawBuffer, m.currentStatus, delta) }
            : m,
        ),
      }));
    };

    const finalize = (status: Status, errorText?: string) => {
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
        trimmed,
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
            // Tentatively record the response id — confirmed on completion.
            set({ previousResponseId: payload.response.id });
          } else if (
            eventType === "response.output_text.delta" &&
            typeof payload?.delta === "string"
          ) {
            appendDelta(payload.delta);
          } else if (eventType === "response.completed") {
            const id = payload?.response?.id;
            if (id) set({ previousResponseId: id });
          } else if (eventType === "error") {
            finalize("error", payload?.message || JSON.stringify(payload));
          }
        },
        ac.signal,
      );
      finalize("done");
    } catch (err) {
      if ((err as any)?.name === "AbortError") {
        finalize("done");
        return;
      }
      finalize("error", err instanceof Error ? err.message : String(err));
    }
  },
}));
