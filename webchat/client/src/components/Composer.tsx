import { useState, useRef, useEffect } from "react";
import { useChatStore } from "../state/chatStore";
import { voice } from "../api/voice";
import { Paperclip, X } from "lucide-react";
import { uploadReference, type ReferenceUpload } from "../api/media";

interface ComposerProps {
  voiceAvailable?: boolean;
  mediaAvailable?: boolean;
  mediaDisabledReason?: string;
}

interface Attachment {
  id: string;
  file: File;
  preview: string;
  progress: number;
  reference?: ReferenceUpload;
  error?: string;
}

export function Composer({ voiceAvailable = false, mediaAvailable = false, mediaDisabledReason }: ComposerProps) {
  const [value, setValue] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploadError, setUploadError] = useState("");
  const [dragging, setDragging] = useState(false);
  const uploading = useRef(false);
  const controllers = useRef(new Map<string, AbortController>());
  const previews = useRef(new Map<string, string>());
  const fileInput = useRef<HTMLInputElement>(null);
  const conversationId = useChatStore((state) => state.conversationId);
  const send = useChatStore((s) => s.send);
  const isStreaming = useChatStore((s) => s.isStreaming);
  const voiceActive = useChatStore((s) => s.voiceActive);
  const voiceStatus = useChatStore((s) => s.voiceStatus);
  const voiceHint = useChatStore((s) => s.voiceHint);
  const ref = useRef<HTMLTextAreaElement>(null);

  const inputLocked = isStreaming || voiceActive;
  const pending = attachments.some((item) => !item.reference);

  useEffect(() => () => {
    controllers.current.forEach((controller) => controller.abort());
    previews.current.forEach((url) => URL.revokeObjectURL(url));
    controllers.current.clear();
    previews.current.clear();
  }, []);

  const remove = (id: string) => {
    controllers.current.get(id)?.abort();
    controllers.current.delete(id);
    const url = previews.current.get(id);
    if (url) URL.revokeObjectURL(url);
    previews.current.delete(id);
    setAttachments((items) => items.filter((item) => item.id !== id));
  };

  const addFiles = async (files: File[]) => {
    if (!mediaAvailable || inputLocked || uploading.current || !files.length) return;
    setUploadError("");
    if (attachments.length + files.length > 4) { setUploadError("At most four references can be attached to a message."); return; }
    if (files.some((file) => !["image/png", "image/jpeg", "image/webp", "video/mp4"].includes(file.type) || file.size === 0 || file.size > 200 * 1024 * 1024)) {
      setUploadError("Choose nonempty PNG, JPEG, WebP or MP4 files, at most 200 MiB each."); return;
    }
    uploading.current = true;
    const additions = files.map((file) => {
      const id = crypto.randomUUID();
      const preview = URL.createObjectURL(file);
      controllers.current.set(id, new AbortController());
      previews.current.set(id, preview);
      return { id, file, preview, progress: 0 };
    });
    setAttachments((items) => [...items, ...additions]);
    try {
      for (const item of additions) {
        const controller = controllers.current.get(item.id);
        if (!controller || controller.signal.aborted) continue;
        try {
          const reference = await uploadReference(item.file, conversationId, controller.signal, (progress) => {
            setAttachments((items) => items.map((current) => current.id === item.id ? { ...current, progress } : current));
          });
          if (!controller.signal.aborted) setAttachments((items) => items.map((current) => current.id === item.id ? { ...current, reference, progress: 100 } : current));
        } catch (error) {
          if (!controller.signal.aborted) setAttachments((items) => items.map((current) => current.id === item.id ? { ...current, error: error instanceof Error ? error.message : "Upload failed" } : current));
        }
      }
    } finally { uploading.current = false; }
  };

  useEffect(() => {
    if (!inputLocked) ref.current?.focus();
  }, [inputLocked]);

  const submit = () => {
    if (inputLocked || pending || (!value.trim() && !attachments.length)) return;
    const text = value;
    const references = attachments.flatMap((item) => item.reference ? [item.reference] : []);
    setValue("");
    attachments.forEach((item) => remove(item.id));
    void send(text, references);
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
    isStreaming || attachments.length > 0 || (voiceActive && voiceStatus !== "speaking");

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
    <div className={`composer-wrap${dragging ? " is-dragging" : ""}`}
      onDragOver={(event) => { event.preventDefault(); if (mediaAvailable && !inputLocked) setDragging(true); }}
      onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragging(false); }}
      onDrop={(event) => { event.preventDefault(); setDragging(false); void addFiles(Array.from(event.dataTransfer.files)); }}>
      {!mediaAvailable && <div className="media-warning" role="status">{mediaDisabledReason ?? "Reference uploads and video controls are unavailable."}</div>}
      {uploadError && <div className="media-error" role="alert">{uploadError}</div>}
      {attachments.length > 0 && <div className="reference-list">
        {attachments.map((item) => <div className="reference-item" key={item.id}>
          {item.file.type === "video/mp4" ? <video src={item.preview} muted playsInline preload="metadata" /> : <img src={item.preview} alt={item.file.name} />}
          <div className="reference-detail"><span>{item.file.name}</span>
            {!item.reference && !item.error && <><progress max={100} value={item.progress} aria-label={`Upload progress: ${item.file.name}`} /><small role="status">{item.progress === 100 ? "Validating / storing..." : `Uploading ${item.progress}%`}</small></>}
            {item.reference && <small>{item.reference.metadata_validated ? "Ready" : "Ready; agent metadata validation pending"}</small>}
            {item.error && <small className="media-error" role="alert">{item.error}</small>}
          </div>
          <button type="button" className="media-icon" title={`Remove ${item.file.name}`} aria-label={`Remove ${item.file.name}`} disabled={inputLocked} onClick={() => remove(item.id)}><X size={16} /></button>
        </div>)}
      </div>}
      {(voiceActive || listening || voiceHint) && (
        <div className={`voice-status voice-status-${voiceStatus}`}>
          {voiceHint ?? micLabel}
        </div>
      )}
      <div className="composer">
        <input ref={fileInput} className="reference-file-input" type="file" multiple accept="image/png,image/jpeg,image/webp,video/mp4" onChange={(event) => { void addFiles(Array.from(event.target.files ?? [])); event.target.value = ""; }} />
        <button type="button" className="media-icon composer-attach" disabled={!mediaAvailable || inputLocked || pending || attachments.length >= 4} onClick={() => fileInput.current?.click()} title="Attach reference" aria-label="Attach reference"><Paperclip size={20} /></button>
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
          disabled={inputLocked || pending || (!value.trim() && !attachments.length)}
        >
          Send
        </button>
      </div>
    </div>
  );
}
