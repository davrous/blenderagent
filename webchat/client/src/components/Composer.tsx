import { useState, useRef, useEffect } from "react";
import { useChatStore } from "../state/chatStore";

export function Composer() {
  const [value, setValue] = useState("");
  const send = useChatStore((s) => s.send);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (!isStreaming) ref.current?.focus();
  }, [isStreaming]);

  const submit = () => {
    if (isStreaming || !value.trim()) return;
    const text = value;
    setValue("");
    void send(text);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="composer">
      <textarea
        ref={ref}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={
          isStreaming ? "Working…" : "Describe a scene, ask for a render, … (Shift+Enter for newline)"
        }
        disabled={isStreaming}
        rows={2}
      />
      <button
        className="composer-send"
        onClick={submit}
        disabled={isStreaming || !value.trim()}
      >
        Send
      </button>
    </div>
  );
}
