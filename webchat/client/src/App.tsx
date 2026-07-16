import { useEffect, useState } from "react";
import { useChatStore } from "./state/chatStore";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { voice } from "./api/voice";

interface Health {
  mode: string;
  agentUrl: string;
  model: string;
  voiceEnabled?: boolean;
}

export function App() {
  const reset = useChatStore((s) => s.reset);
  const messages = useChatStore((s) => s.messages);
  const [health, setHealth] = useState<Health | null>(null);

  useEffect(() => {
    fetch("/api/health")
      .then((r) => r.json())
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  const voiceAvailable = !!health?.voiceEnabled && voice.isSupported();

  // Wire the voice controller to the chat store once voice is available.
  useEffect(() => {
    if (!voiceAvailable) return;
    voice.init({
      onStatus: (s) => useChatStore.getState().setVoiceStatus(s),
      onUserTranscript: (t) => useChatStore.getState().voiceBeginTurn(t),
      onAgentDelta: (d) => useChatStore.getState().voiceAppendDelta(d),
      onAgentDone: (rid) => useChatStore.getState().voiceFinalizeTurn(rid),
      onError: (m) => useChatStore.getState().voiceFail(m),
      getConversationId: () => useChatStore.getState().conversationId,
      getPreviousResponseId: () => useChatStore.getState().previousResponseId,
    });
    // Warm up the voice connection now so the hosted container cold-starts in
    // the background — otherwise the FIRST mic press is lost to invocations_ws
    // cold-start and the user has to press twice.
    void voice.prewarm();
    return () => voice.dispose();
  }, [voiceAvailable]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-title">
          <span className="app-logo" aria-hidden>🎬</span>
          <span>Blender Scene Agent</span>
        </div>
        <div className="app-meta">
          {health && (
            <span className={`mode-badge mode-${health.mode}`}>
              {health.mode} · {health.model}
            </span>
          )}
          <button
            className="reset-btn"
            onClick={reset}
            disabled={messages.length === 0}
            title="Start a new conversation"
          >
            Reset
          </button>
        </div>
      </header>
      <main className="app-main">
        <ChatView />
      </main>
      <footer className="app-footer">
        <Composer voiceAvailable={voiceAvailable} />
      </footer>
    </div>
  );
}
