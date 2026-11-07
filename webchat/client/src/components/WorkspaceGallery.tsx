import { useEffect, useState } from "react";
import { useChatStore } from "../state/chatStore";
import { createWorkspace, listWorkspaces, type WorkspaceSummary } from "../api/workspaces";
import { blobProxyUrl, loadJobIds, requestVideoJob } from "../api/media";

export function WorkspaceGallery() {
  const start = useChatStore((state) => state.startWorkspace);
  const resume = useChatStore((state) => state.resumeWorkspace);
  const busy = useChatStore((state) => state.workspaceLoading);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);

  useEffect(() => {
    let disposed = false;
    setLoading(true);
    void (async () => {
      try {
        let entries = await listWorkspaces();
        if (!disposed) setWorkspaces(entries);
        const known = new Set(entries.map((entry) => entry.id));
        const ids = new Set<string>();
        const current = localStorage.getItem("webchat.conversationId");
        if (current) ids.add(current);
        for (let index = 0; index < localStorage.length; index++) {
          const key = localStorage.key(index);
          if (key?.startsWith("webchat.videoJobs.")) ids.add(key.slice("webchat.videoJobs.".length));
        }
        const warnings: string[] = [];
        for (const id of ids) {
          if (known.has(id) || !/^[0-9a-f-]{36}$/i.test(id)) continue;
          const jobs = loadJobIds(id);
          if (!jobs.length && id !== current) continue;
          try {
            await createWorkspace(id);
            for (const job of jobs) await requestVideoJob(id, job);
          } catch (failure) {
            console.warn("Legacy workspace import failed", failure);
            warnings.push(`Import partiel de ${id}: ${failure instanceof Error ? failure.message : "session indisponible"}`);
          }
        }
        entries = await listWorkspaces();
        if (!disposed) { setWorkspaces(entries); setError(warnings.join(" · ")); }
      } catch (failure) {
        if (!disposed) setError(failure instanceof Error ? failure.message : "Impossible de charger les réalisations.");
      } finally { if (!disposed) setLoading(false); }
    })();
    return () => { disposed = true; };
  }, [refresh]);

  return <section className="workspace-home">
    <div className="workspace-heading">
      <div><h1>Mes réalisations Blender</h1><p>Reprends un travail et ses ressources, ou pars d'une scène vierge.</p></div>
      <button className="media-command" disabled={busy} onClick={() => void start()}>＋ Nouvelle scène</button>
    </div>
    {loading && <p role="status">Chargement des réalisations et import des sessions encore disponibles…</p>}
    {error && <p className="media-error" role="alert">{error} <button disabled={busy} onClick={() => setRefresh((value) => value + 1)}>Réessayer</button></p>}
    {!loading && !error && workspaces.length === 0 && <p>Aucune réalisation archivée. Crée ta première scène.</p>}
    <div className="workspace-grid">
      {workspaces.map((workspace) => <button className="workspace-tile" key={workspace.id} disabled={busy} onClick={() => void resume(workspace.id)}>
        {workspace.thumbnail_url ? <img src={blobProxyUrl(workspace.thumbnail_url)} alt={`Aperçu de ${workspace.title}`} loading="lazy" /> : <div className="workspace-placeholder" aria-hidden>🎬</div>}
        <strong>{workspace.title}</strong>
        <small>{new Date(workspace.updated_at).toLocaleString("fr-FR")} · {workspace.resource_count} ressource(s)</small>
        <span>Reprendre le travail →</span>
      </button>)}
    </div>
  </section>;
}
