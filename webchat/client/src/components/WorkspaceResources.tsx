import { useEffect, useState } from "react";
import { loadWorkspace, type WorkspaceResource } from "../api/workspaces";
import { blobProxyUrl } from "../api/media";
import { ImageLightbox } from "./ImageLightbox";

export function WorkspaceResources({ conversationId }: { conversationId: string }) {
  const [resources, setResources] = useState<WorkspaceResource[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [lightbox, setLightbox] = useState<WorkspaceResource | null>(null);
  useEffect(() => {
    let disposed = false;
    setLoading(true);
    void loadWorkspace(conversationId).then((workspace) => {
      if (!disposed) { setResources(workspace.resources.sort((a, b) => a.created_at - b.created_at)); setError(""); }
    }).catch((failure) => {
      if (!disposed) setError(failure instanceof Error ? failure.message : "Impossible de lire les ressources.");
    }).finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [conversationId]);
  return <section className="workspace-resources">
    <h2>Toute la séquence de cette réalisation</h2>
    <p>Extraits audio, images, animations et fichiers Blender dans l'ordre de création.</p>
    {loading && <p role="status">Chargement et renouvellement des liens privés…</p>}
    {error && <p className="media-error" role="alert">{error}</p>}
    {!loading && !resources.length && !error && <p>Aucune ressource générée pour cette réalisation.</p>}
    <div className="workspace-grid">{resources.map((resource) => <article className="workspace-resource" key={resource.id}>
      <strong>{resource.name}</strong>
      <small>{new Date(resource.created_at).toLocaleString("fr-FR")}</small>
      {resource.kind === "image" && <button className="msg-image-btn" onClick={() => setLightbox(resource)}><img src={blobProxyUrl(resource.url)} alt={resource.name} loading="lazy" /></button>}
      {resource.kind === "video" && <video controls playsInline preload="metadata" src={blobProxyUrl(resource.url)} />}
      {resource.kind === "audio" && <audio controls preload="metadata" src={blobProxyUrl(resource.url)} />}
      <a className="video-download" href={blobProxyUrl(resource.url)} download={resource.name}>Télécharger {resource.kind === "blend" ? "le fichier Blender" : "la ressource"}</a>
    </article>)}</div>
    {lightbox && <ImageLightbox src={blobProxyUrl(lightbox.url)} alt={lightbox.name} onClose={() => setLightbox(null)} />}
  </section>;
}
