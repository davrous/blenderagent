import { useEffect, useState } from "react";
import { useChatStore } from "./state/chatStore";
import { ChatView } from "./components/ChatView";
import { Composer } from "./components/Composer";
import { voice } from "./api/voice";
import { loadJobIds } from "./api/media";
import { loadHealth, type Health } from "./api/health";
import { WorkspaceGallery } from "./components/WorkspaceGallery";
import { WorkspaceResources } from "./components/WorkspaceResources";

export function App() {
  const reset = useChatStore((s) => s.reset);
  const messages = useChatStore((s) => s.messages);
  const conversationId = useChatStore((state) => state.conversationId);
  const workspaceView = useChatStore((state) => state.workspaceView);
  const workspaceLoading = useChatStore((state) => state.workspaceLoading);
  const workspaceError = useChatStore((state) => state.workspaceError);
  const workspaceBinding = useChatStore((state) => state.workspaceBinding);
  const startWorkspace = useChatStore((state) => state.startWorkspace);
  const showGallery = useChatStore((state) => state.showGallery);
  const showResources = useChatStore((state) => state.showResources);
  const showChat = useChatStore((state) => state.showChat);
  const busy = useChatStore((state) => state.isStreaming || state.voiceActive || ["connecting", "listening", "thinking", "analyzing"].includes(state.voiceStatus));
  const [health, setHealth] = useState<Health | null>(null);
  const [connectionStatus, setConnectionStatus] = useState("Connecting to Webchat...");

  useEffect(() => loadHealth(setHealth, () => {
    setConnectionStatus("Webchat proxy is unavailable. Reconnecting...");
  }), []);

  const voiceAvailable = !!health?.voiceEnabled && voice.isSupported();
  const galleryAvailable = !!health?.galleryEnabled;
  const chatting = !galleryAvailable || workspaceView === "chat";

  // Wire the voice controller to the chat store once voice is available.
  useEffect(() => {
    if (!voiceAvailable || !chatting || workspaceBinding?.expired) return;
    voice.init({
      onStatus: (s) => useChatStore.getState().setVoiceStatus(s),
      onUserTranscript: (t) => useChatStore.getState().voiceBeginTurn(t),
      onAgentDelta: (d) => useChatStore.getState().voiceAppendDelta(d),
      onAgentDone: (rid) => useChatStore.getState().voiceFinalizeTurn(rid),
      onMusicResult: (candidate) => useChatStore.getState().setMusicCandidate(candidate),
      onInputAudio: (url) => useChatStore.getState().voiceInputAudio(url),
      onArchiveWarning: (message) => useChatStore.getState().setWorkspaceError(message),
      onError: (m) => useChatStore.getState().voiceFail(m),
      getConversationId: () => useChatStore.getState().conversationId,
      getPreviousResponseId: () => useChatStore.getState().previousResponseId,
    });
    // Warm up the voice connection now so the hosted container cold-starts in
    // the background — otherwise the FIRST mic press is lost to invocations_ws
    // cold-start and the user has to press twice. Defer one event-loop turn so
    // React StrictMode's development-only mount → cleanup → mount probe cancels
    // its throwaway effect before it can open a duplicate WebSocket.
    const prewarmTimer = window.setTimeout(() => void voice.prewarm(), 0);
    return () => {
      window.clearTimeout(prewarmTimer);
      voice.dispose();
    };
  }, [voiceAvailable, chatting, conversationId, workspaceBinding?.expired]);

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
          {galleryAvailable && <button className="reset-btn" onClick={showGallery} disabled={busy || workspaceLoading}>Réalisations</button>}
          {galleryAvailable && workspaceView !== "gallery" && <>
            <button className="reset-btn" disabled={busy || workspaceLoading || !!workspaceBinding?.expired} onClick={showChat}>Discussion</button>
            <button className="reset-btn" disabled={busy || workspaceLoading} onClick={showResources}>Ressources</button>
          </>}
          <button
            className="reset-btn"
            onClick={() => { if (galleryAvailable) void startWorkspace(); else reset(); }}
            disabled={galleryAvailable ? busy || workspaceLoading : messages.length === 0 && loadJobIds(conversationId).length === 0}
            title="Start a new conversation"
          >
            {galleryAvailable ? "Nouvelle scène" : "Reset"}
          </button>
        </div>
      </header>
      <main className="app-main">
        {workspaceLoading && <p className="workspace-loading" role="status">Ouverture du travail et restauration de ses associations…</p>}
        {workspaceError && <p className="media-error workspace-alert" role="alert">{workspaceError}</p>}
        {workspaceBinding?.expired && galleryAvailable && workspaceView !== "gallery" && <p className="media-warning workspace-alert">Le sandbox a expiré. Les contenus archivés restent disponibles en lecture.</p>}
        {galleryAvailable && workspaceView === "gallery" ? <WorkspaceGallery />
          : galleryAvailable && workspaceView === "resources" ? <WorkspaceResources key={conversationId} conversationId={conversationId} />
            : <ChatView mediaAvailable={!!health?.mediaEnabled} />}
      </main>
      {chatting && <footer className="app-footer">
        <Composer key={conversationId} voiceAvailable={voiceAvailable} musicAvailable={voiceAvailable && !!health?.musicEnabled} mediaAvailable={!!health?.mediaEnabled} mediaDisabledReason={health ? health.mediaDisabledReason : connectionStatus} />
      </footer>}
    </div>
  );
}
