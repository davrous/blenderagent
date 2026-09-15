import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import clsx from "clsx";
import { useChatStore, type Message } from "../state/chatStore";
import { StatusPill } from "./StatusPill";
import { ImageLightbox } from "./ImageLightbox";
import { DownloadButton } from "./DownloadButton";
import { AssetGallery } from "./AssetGallery";
import {
  isDownloadLink,
  dedupeMarkdownMedia,
  extractGalleries,
  stripGalleryBlocks,
  isVideoLink,
  extractVideoJobIds,
  stripVideoJobBlocks,
} from "../lib/parseMarkdown";
import { blobProxyUrl } from "../api/media";

interface Props {
  message: Message;
}

function MessageBubbleImpl({ message }: Props) {
  const [lightbox, setLightbox] = useState<{ src: string; alt?: string } | null>(null);
  const selectModel = useChatStore((s) => s.selectModel);
  const selectTexture = useChatStore((s) => s.selectTexture);
  const busy = useChatStore((s) => s.isStreaming || s.voiceActive);

  if (message.role === "user") {
    return (
      <div className="msg msg-user">
        <div className="msg-bubble">{message.text}{message.referenceNames?.map((name, index) => <div className="reference-name" key={`${name}-${index}`}>{name}</div>)}</div>
      </div>
    );
  }

  const galleries = extractGalleries(message.text);
  const jobIds = extractVideoJobIds(message.text);
  const prose = dedupeMarkdownMedia(stripGalleryBlocks(stripVideoJobBlocks(message.text)));

  if (jobIds.length && !prose && !galleries.length && message.status === "done") return null;

  return (
    <div className={clsx("msg msg-assistant", `msg-${message.status}`)}>
      <div className="msg-bubble">
        {message.status === "streaming" && message.currentStatus && (
          <StatusPill status={message.currentStatus} />
        )}

        {message.status === "error" && (
          <div className="msg-error">⚠️ {message.errorText ?? "Error"}</div>
        )}

        {prose && (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              img: ({ src, alt }) => {
                if (!src) return null;
                return (
                  <button
                    type="button"
                    className="msg-image-btn"
                    onClick={() => setLightbox({ src, alt })}
                  >
                    <img src={src} alt={alt ?? ""} className="msg-image" />
                  </button>
                );
              },
              a: ({ href, children }) => {
                if (href && isVideoLink(href)) return <video className="inline-video" controls playsInline preload="metadata" src={blobProxyUrl(href)} />;
                if (href && isDownloadLink(href)) {
                  const label =
                    typeof children === "string"
                      ? children
                      : Array.isArray(children)
                        ? children.filter((c) => typeof c === "string").join("")
                        : undefined;
                  return <DownloadButton href={href} label={label} />;
                }
                return (
                  <a href={href} target="_blank" rel="noreferrer">
                    {children}
                  </a>
                );
              },
            }}
          >
            {prose}
          </ReactMarkdown>
        )}

        {galleries.map((gallery, i) => (
          <AssetGallery
            key={`${gallery.kind}-${i}`}
            gallery={gallery}
            disabled={busy}
            onSelectModel={(m) => selectModel(m.modelUrl, m.name)}
            onSelectTexture={(t) => selectTexture(t.assetId, t.name)}
          />
        ))}

        {message.status === "streaming" && !prose && !galleries.length && !jobIds.length && !message.currentStatus && (
          <div className="msg-thinking">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
          </div>
        )}

        {message.status === "done" && !prose && !galleries.length && !jobIds.length && !message.errorText && (
          <div className="msg-empty">
            <em>(no response)</em>
          </div>
        )}
      </div>

      {lightbox && (
        <ImageLightbox
          src={lightbox.src}
          alt={lightbox.alt}
          onClose={() => setLightbox(null)}
        />
      )}
    </div>
  );
}

export const MessageBubble = memo(MessageBubbleImpl);

