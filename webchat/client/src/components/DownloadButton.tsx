import { filenameFromUrl } from "../lib/parseMarkdown";

interface Props {
  href: string;
  label?: string;
}

export function DownloadButton({ href, label }: Props) {
  const filename = filenameFromUrl(href);
  const ext = filename.split(".").pop()?.toUpperCase() ?? "FILE";
  return (
    <a className="download-btn" href={href} target="_blank" rel="noreferrer" download>
      <span className="download-btn-icon" aria-hidden>
        ⬇
      </span>
      <span className="download-btn-text">
        <span className="download-btn-label">{label ?? "Download"}</span>
        <span className="download-btn-meta">
          {ext} · {filename}
        </span>
      </span>
    </a>
  );
}
