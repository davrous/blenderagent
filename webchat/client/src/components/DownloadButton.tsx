import { useState } from "react";
import { filenameFromUrl } from "../lib/parseMarkdown";
import { BabylonViewer } from "./BabylonViewer";

interface Props {
  href: string;
  label?: string;
}

export function DownloadButton({ href, label }: Props) {
  const filename = filenameFromUrl(href);
  const ext = filename.split(".").pop()?.toLowerCase() ?? "";
  const extUpper = ext.toUpperCase() || "FILE";
  const canPreview3D = ext === "glb" || ext === "gltf";
  const [viewerOpen, setViewerOpen] = useState(false);

  return (
    <>
      <span className="download-btn-row">
        <a className="download-btn" href={href} target="_blank" rel="noreferrer" download>
          <span className="download-btn-icon" aria-hidden>
            ⬇
          </span>
          <span className="download-btn-text">
            <span className="download-btn-label">{label ?? "Download"}</span>
            <span className="download-btn-meta">
              {extUpper} · {filename}
            </span>
          </span>
        </a>
        {canPreview3D && (
          <button
            type="button"
            className="view3d-btn"
            onClick={() => setViewerOpen(true)}
            title="Open in interactive 3D viewer"
          >
            <span className="view3d-btn-icon" aria-hidden>
              🧊
            </span>
            <span className="view3d-btn-text">
              <span className="view3d-btn-label">View 3D scene</span>
              <span className="view3d-btn-meta">in Babylon.js</span>
            </span>
          </button>
        )}
      </span>
      {viewerOpen && canPreview3D && (
        <BabylonViewer
          src={href}
          filename={filename}
          onClose={() => setViewerOpen(false)}
        />
      )}
    </>
  );
}
