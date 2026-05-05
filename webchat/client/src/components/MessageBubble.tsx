import { memo, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import clsx from "clsx";
import type { Message } from "../state/chatStore";
import { StatusPill } from "./StatusPill";
import { ImageLightbox } from "./ImageLightbox";
import { DownloadButton } from "./DownloadButton";
import { isDownloadLink } from "../lib/parseMarkdown";

interface Props {
  message: Message;
}

function MessageBubbleImpl({ message }: Props) {
  const [lightbox, setLightbox] = useState<{ src: string; alt?: string } | null>(null);

  if (message.role === "user") {
    return (
      <div className="msg msg-user">
        <div className="msg-bubble">{message.text}</div>
      </div>
    );
  }

  return (
    <div className={clsx("msg msg-assistant", `msg-${message.status}`)}>
      <div className="msg-bubble">
        {message.status === "streaming" && message.currentStatus && (
          <StatusPill status={message.currentStatus} />
        )}

        {message.status === "error" && (
          <div className="msg-error">⚠️ {message.errorText ?? "Error"}</div>
        )}

        {message.text && (
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
            {message.text}
          </ReactMarkdown>
        )}

        {message.status === "streaming" && !message.text && !message.currentStatus && (
          <div className="msg-thinking">
            <span className="dot" />
            <span className="dot" />
            <span className="dot" />
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
