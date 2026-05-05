import { useEffect, useRef } from "react";
import { useChatStore } from "../state/chatStore";
import { MessageBubble } from "./MessageBubble";

const STICK_THRESHOLD_PX = 32;

export function ChatView() {
  const messages = useChatStore((s) => s.messages);
  const contentRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const prevLenRef = useRef(messages.length);

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

  if (messages.length === 0) {
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
      {messages.map((m) => (
        <MessageBubble key={m.id} message={m} />
      ))}
    </div>
  );
}
