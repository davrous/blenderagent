import { useEffect, useState } from "react";
import { useChatStore } from "../state/chatStore";
import { createWorkspace, deleteWorkspace, listWorkspaces, renameWorkspace, type WorkspaceSummary } from "../api/workspaces";
import { blobProxyUrl, loadJobIds, requestVideoJob } from "../api/media";

export function WorkspaceGallery() {
  const start = useChatStore((state) => state.startWorkspace);
  const resume = useChatStore((state) => state.resumeWorkspace);
  const busy = useChatStore((state) => state.workspaceLoading || state.isStreaming || state.voiceActive);
  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [pending, setPending] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [title, setTitle] = useState("");
  const disabled = busy || !!pending;

  const forgetLocalWorkspace = (id: string) => {
    localStorage.removeItem(`webchat.videoJobs.${id}`);
    if (localStorage.getItem("webchat.conversationId") === id) {
      localStorage.removeItem("webchat.conversationId");
      localStorage.removeItem("webchat.previousResponseId");
    }
  };
  const remove = async (workspace: WorkspaceSummary) => {
    if (!window.confirm(`Supprimer définitivement « ${workspace.title} » ?\n\nL'historique, les fichiers propres à cette réalisation et le sandbox Foundry seront supprimés. Cette action est irréversible.`)) return;
    setPending(workspace.id); setError("");
    try {
      await deleteWorkspace(workspace.id);
      forgetLocalWorkspace(workspace.id);
      setWorkspaces((entries) => entries.filter((entry) => entry.id !== workspace.id));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "La suppression a échoué.");
      try { setWorkspaces(await listWorkspaces()); } catch {}
    } finally { setPending(null); }
  };
  const rename = async (workspace: WorkspaceSummary) => {
    setPending(workspace.id); setError("");
    try {
      await renameWorkspace(workspace.id, title);
      setWorkspaces((entries) => entries.map((entry) => entry.id === workspace.id ? { ...entry, title: title.trim().replace(/\s+/g, " ") } : entry));
      setEditing(null);
    } catch (failure) { setError(failure instanceof Error ? failure.message : "Le titre n'a pas pu être enregistré."); }
    finally { setPending(null); }
  };

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
      <button className="media-command" disabled={disabled} onClick={() => void start()}>＋ Nouvelle scène</button>
    </div>
    {loading && <p role="status">Chargement des réalisations et import des sessions encore disponibles…</p>}
    {error && <p className="media-error" role="alert">{error} <button disabled={disabled} onClick={() => setRefresh((value) => value + 1)}>Réessayer</button></p>}
    {!loading && !error && workspaces.length === 0 && <p>Aucune réalisation archivée. Crée ta première scène.</p>}
    <div className="workspace-grid">
      {workspaces.map((workspace) => <article className="workspace-tile" key={workspace.id}>
        <div className="workspace-thumbnail">
          <button className="workspace-open" disabled={disabled || workspace.deletion_pending} aria-label={`Reprendre ${workspace.title}`} onClick={() => void resume(workspace.id)}>
            {workspace.thumbnail_url ? <img src={blobProxyUrl(workspace.thumbnail_url)} alt={`Aperçu de ${workspace.title}`} loading="lazy" /> : <div className="workspace-placeholder" aria-hidden>🎬</div>}
          </button>
          <button className="workspace-delete" type="button" disabled={disabled} aria-label={`Supprimer ${workspace.title}`} title="Supprimer définitivement" onClick={() => void remove(workspace)}>🗑</button>
        </div>
        {editing === workspace.id ? <form className="workspace-title-editor" onSubmit={(event) => { event.preventDefault(); void rename(workspace); }}>
          <input autoFocus aria-label="Titre de la réalisation" maxLength={90} value={title} disabled={disabled} onChange={(event) => setTitle(event.target.value)} />
          <button type="submit" disabled={disabled || !title.trim()}>Enregistrer</button>
          <button type="button" disabled={disabled} onClick={() => setEditing(null)}>Annuler</button>
        </form> : <div className="workspace-title">
          <strong>{workspace.title}</strong>
          <button type="button" disabled={disabled || workspace.deletion_pending} aria-label={`Modifier le titre de ${workspace.title}`} onClick={() => { setEditing(workspace.id); setTitle(workspace.title); }}>✎</button>
        </div>}
        <small>{new Date(workspace.updated_at).toLocaleString("fr-FR")} · {workspace.resource_count} ressource(s)</small>
        {workspace.deletion_pending ? <p className="media-error" role="status">Suppression enregistrée ; nettoyage incomplet. Utilise la corbeille pour réessayer.</p>
          : <button className="workspace-resume" disabled={disabled} onClick={() => void resume(workspace.id)}>Reprendre le travail →</button>}
      </article>)}
    </div>
  </section>;
}
