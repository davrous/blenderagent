import { useEffect, useRef } from "react";
import { useChatStore } from "../state/chatStore";
import { MessageBubble } from "./MessageBubble";

export function ChatView() {
  const messages = useChatStore((s) => s.messages);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  if (messages.length === 0) {
    return (
      <div className="chat-empty">
        <h2>Blender Scene Agent</h2>
        <p>
          Try: <em>“Create a red cube on a wooden floor and screenshot it.”</em>
        </p>
      </div>
    );
  }

  return (
    <div className="chat-view">
      {messages.map((m) => (
        <MessageBubble key={m.id} message={m} />
      ))}
      <div ref={endRef} />
    </div>
  );
}
