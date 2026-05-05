import { useEffect, useState } from "react";
import { useChatStore } from "./state/chatStore";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";

interface Health {
  mode: string;
  agentUrl: string;
  model: string;
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
        <Composer />
      </footer>
    </div>
  );
}
