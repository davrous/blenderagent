import { create } from "zustand";
import { streamChat } from "../api/stream";

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
  isStreaming: boolean;
  abortController: AbortController | null;
  send: (input: string) => Promise<void>;
  reset: () => void;
}

// Matches a complete italic status block surrounded by blank lines:
// "\n\n*…some text…*\n\n" — used by ToolStatusMiddleware.
// We require both delimiters so we never extract partial deltas.
const STATUS_BLOCK_RE = /\n{2}\*([^*\n]+)\*\n{2}/;

let idCounter = 0;
const newId = () => `m${Date.now()}-${++idCounter}`;

export const useChatStore = create<ChatState>((set, get) => ({
  messages: [],
  previousResponseId: null,
  isStreaming: false,
  abortController: null,

  reset: () => {
    get().abortController?.abort();
    set({
      messages: [],
      previousResponseId: null,
      isStreaming: false,
      abortController: null,
    });
  },

  send: async (input) => {
    if (get().isStreaming) return;
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
        messages: state.messages.map((m) => {
          if (m.id !== assistantId) return m;
          let raw = m.rawBuffer + delta;
          let status = m.currentStatus;
          // Extract any complete status blocks from the buffer.
          let match: RegExpMatchArray | null;
          while ((match = raw.match(STATUS_BLOCK_RE)) !== null) {
            status = match[1].trim();
            const start = match.index!;
            const end = start + match[0].length;
            // Replace the block with a single blank line so paragraphs stay separated.
            raw = raw.slice(0, start) + "\n\n" + raw.slice(end);
          }
          return { ...m, rawBuffer: raw, text: raw, currentStatus: status };
        }),
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
