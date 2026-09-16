import { useEffect, useRef, useState } from "react";
import { useChatStore } from "../state/chatStore";
import { MessageBubble } from "./MessageBubble";
import { VideoJobCard } from "./VideoJobCard";
import { buildChatTimeline } from "../lib/parseMarkdown";
import { loadJobIds, saveJobIds } from "../api/media";

const STICK_THRESHOLD_PX = 32;

export function ChatView({ mediaAvailable = false }: { mediaAvailable?: boolean }) {
  const conversationId = useChatStore((state) => state.conversationId);
  return <ChatTimeline key={conversationId} conversationId={conversationId} mediaAvailable={mediaAvailable} />;
}

function ChatTimeline({ conversationId, mediaAvailable }: { conversationId: string; mediaAvailable: boolean }) {
  const messages = useChatStore((state) => state.messages);
  const [recoveredIds] = useState(() => {
    const visibleIds = new Set(buildChatTimeline(messages).jobIds);
    return loadJobIds(conversationId).filter((id) => !visibleIds.has(id));
  });
  const [storageError, setStorageError] = useState(false);
  const { entries, jobIds } = buildChatTimeline(messages, recoveredIds);
  const jobKey = jobIds.join(",");
  const contentRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const prevLenRef = useRef(messages.length);

  useEffect(() => {
    try {
      saveJobIds(conversationId, jobKey ? jobKey.split(",") : []);
      setStorageError(false);
    } catch { setStorageError(true); }
  }, [conversationId, jobKey]);

  // Force snap-to-bottom whenever a new user message is appended.
  if (messages.length > prevLenRef.current) {
    const last = messages[messages.length - 1];
    if (last?.role === "user") {
      stickRef.current = true;
    }
  }
  prevLenRef.current = messages.length;

  // Watch the scroll container and the content size; pin to bottom whenever
  // either changes, as long as the user is still anchored at the bottom.
  useEffect(() => {
    const content = contentRef.current;
    if (!content) return;
    const scroller = content.closest(".app-main") as HTMLElement | null;
    if (!scroller) return;

    const isAtBottom = () => {
      const distance =
        scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      return distance < STICK_THRESHOLD_PX;
    };

    const pin = () => {
      if (!stickRef.current) return;
      // Use scrollTop assignment (instant) — smooth scroll stacks animations
      // and causes visible flicker during streaming.
      scroller.scrollTop = scroller.scrollHeight;
    };

    const onScroll = () => {
      stickRef.current = isAtBottom();
    };

    scroller.addEventListener("scroll", onScroll, { passive: true });

    // Re-pin whenever content grows (new tokens, status pills, lazy images
    // finishing load, markdown reflow, etc.).
    const ro = new ResizeObserver(() => {
      pin();
    });
    ro.observe(content);

    // Also re-pin when individual images inside the content finish loading,
    // since lazy images can change layout after ResizeObserver has settled.
    const onLoadCapture = (e: Event) => {
      if ((e.target as HTMLElement)?.tagName === "IMG") pin();
    };
    content.addEventListener("load", onLoadCapture, true);

    // Initial pin on mount.
    pin();

    return () => {
      scroller.removeEventListener("scroll", onScroll);
      content.removeEventListener("load", onLoadCapture, true);
      ro.disconnect();
    };
  }, []);

  if (entries.length === 0) {
    return (
      <div className="chat-view chat-view-empty" ref={contentRef}>
        <div className="chat-empty">
          <h2>Blender Scene Agent</h2>
          <p>
            Try: <em>“Create a red cube on a wooden floor and screenshot it.”</em>
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="chat-view" ref={contentRef}>
      {storageError && jobIds.length > 0 && <p className="media-warning">Browser storage is unavailable. Job IDs will not survive a reload.</p>}
      {entries.map((entry) => entry.kind === "message"
        ? <MessageBubble key={`message:${entry.message.id}`} message={entry.message} />
        : <VideoJobCard key={`video:${entry.id}`} id={entry.id} conversationId={conversationId} enabled={mediaAvailable} />)}
    </div>
  );
}
