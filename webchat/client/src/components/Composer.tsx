import { useState, useRef, useEffect } from "react";
import { useChatStore } from "../state/chatStore";
import { voice } from "../api/voice";

interface ComposerProps {
  voiceAvailable?: boolean;
}

export function Composer({ voiceAvailable = false }: ComposerProps) {
  const [value, setValue] = useState("");
  const send = useChatStore((s) => s.send);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const voiceActive = useChatStore((s) => s.voiceActive);
  const voiceStatus = useChatStore((s) => s.voiceStatus);
  const voiceHint = useChatStore((s) => s.voiceHint);
  const ref = useRef<HTMLTextAreaElement>(null);

  const inputLocked = isStreaming || voiceActive;

  useEffect(() => {
    if (!inputLocked) ref.current?.focus();
  }, [inputLocked]);

  const submit = () => {
    if (inputLocked || !value.trim()) return;
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

  // Press-and-hold to talk. Pointer capture keeps the release event bound to
  // the button even if the pointer drifts off it while held.
  const listening = voiceStatus === "listening";
  const micDisabled =
    isStreaming || (voiceActive && voiceStatus !== "speaking");

  const micLabel = listening
    ? "Listening… release to send"
    : voiceStatus === "thinking"
      ? "Thinking…"
      : voiceStatus === "speaking"
        ? "Speaking… hold to interrupt"
        : "Hold to talk";

  const onMicDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (micDisabled) return;
    e.preventDefault();
    e.currentTarget.setPointerCapture?.(e.pointerId);
    void voice.startListening();
  };

  const onMicUp = (e: React.PointerEvent<HTMLButtonElement>) => {
    e.currentTarget.releasePointerCapture?.(e.pointerId);
    if (listening) void voice.stopAndCommit();
  };

  const onMicCancel = () => {
    if (listening) voice.cancel();
  };

  return (
    <div className="composer-wrap">
      {(voiceActive || listening || voiceHint) && (
        <div className={`voice-status voice-status-${voiceStatus}`}>
          {voiceHint ?? micLabel}
        </div>
      )}
      <div className="composer">
        {voiceAvailable && (
          <button
            type="button"
            className={`composer-mic${listening ? " is-listening" : ""}`}
            onPointerDown={onMicDown}
            onPointerUp={onMicUp}
            onPointerCancel={onMicCancel}
            disabled={micDisabled}
            title={micLabel}
            aria-label={micLabel}
            aria-pressed={listening}
          >
            <span aria-hidden>{listening ? "●" : "🎙️"}</span>
          </button>
        )}
        <textarea
          ref={ref}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            inputLocked
              ? "Working…"
              : "Describe a scene, ask for a render, … (Shift+Enter for newline)"
          }
          disabled={inputLocked}
          rows={2}
        />
        <button
          className="composer-send"
          onClick={submit}
          disabled={inputLocked || !value.trim()}
        >
          Send
        </button>
      </div>
    </div>
  );
}
