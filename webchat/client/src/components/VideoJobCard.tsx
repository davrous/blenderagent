import { useEffect, useRef, useState } from "react";
import { Check, Download, Loader2, RefreshCw, X } from "lucide-react";
import { useChatStore } from "../state/chatStore";
import { blobProxyUrl, paidEstimate, requestVideoJob, RESOLUTION_RATES, TERMINAL_JOB_STATES, type VideoJob, type VideoResolution, type ApprovalOptions, type RenderOptions } from "../api/media";
import { ImageLightbox } from "./ImageLightbox";

const paused = (state: string) => TERMINAL_JOB_STATES.has(state) || state === "awaiting_render_approval";

export function VideoJobCard({ id, conversationId, enabled }: { id: string; conversationId: string; enabled: boolean }) {
  const busy = useChatStore((state) => state.isStreaming || state.voiceActive);
  const [job, setJob] = useState<VideoJob | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [pendingAction, setPendingAction] = useState<{ type: "approve" | "cancel" | "render"; resolution: RenderOptions["resolution"] } | null>(null);
  const acting = pendingAction !== null;
  const actionInFlight = useRef(false);
  const [viewportOpen, setViewportOpen] = useState(false);
  const [viewportError, setViewportError] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [prompt, setPrompt] = useState("");
  const [resolution, setResolution] = useState<VideoResolution>("720p");
  const [audio, setAudio] = useState(false);
  const [consent, setConsent] = useState(false);
  const [inspected, setInspected] = useState(false);
  const [hdConsent, setHdConsent] = useState(false);
  const stopped = useRef(false);

  const retry = () => { stopped.current = false; setError(""); setConsent(false); setInspected(false); setHdConsent(false); setRefresh((value) => value + 1); };

  useEffect(() => {
    if (!enabled || busy || acting || stopped.current) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      setLoading(true);
      try {
        const latest = await requestVideoJob(conversationId, id);
        if (disposed) return;
        setJob(latest);
        setError("");
        stopped.current = paused(latest.state);
        if (!paused(latest.state)) timer = setTimeout(() => { void poll(); }, 7500);
      } catch (failure) {
        if (!disposed) { stopped.current = true; setError(failure instanceof Error ? failure.message : "Status check failed"); }
      } finally { if (!disposed) setLoading(false); }
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [conversationId, id, enabled, busy, acting, refresh]);

  const act = async (action: "approve" | "cancel" | "render", nativeResolution: RenderOptions["resolution"] = "480p") => {
    const current = useChatStore.getState();
    if (!enabled || loading || actionInFlight.current || current.isStreaming || current.voiceActive || error || !job) return;
    if (action === "approve" && (!consent || !job.seedance_enabled || job.state !== "awaiting_seedance_approval")) return;
    if (action === "render" && (!job.inspection_required || !job.scene_url
      || (nativeResolution === "480p" ? !inspected || job.state !== "awaiting_render_approval" : !hdConsent || !job.hd_available))) return;
    const options: ApprovalOptions | RenderOptions | undefined = action === "approve"
      ? { prompt, resolution, generate_audio: audio }
      : action === "render" ? { resolution: nativeResolution } : undefined;
    actionInFlight.current = true;
    setPendingAction({ type: action, resolution: nativeResolution });
    setConsent(false);
    setInspected(false);
    setHdConsent(false);
    try {
      const latest = await requestVideoJob(conversationId, id, action, options);
      stopped.current = paused(latest.state);
      setJob(latest);
    } catch (failure) {
      stopped.current = true;
      setError(`${failure instanceof Error ? failure.message : "Action failed"} Refresh status to check the outcome; this action was not retried.`);
    } finally { actionInFlight.current = false; setLoading(false); setPendingAction(null); }
  };

  const locked = busy || loading || acting || !enabled || !!error;
  const state = job?.state ?? "loading";
  const estimate = job ? paidEstimate(job.duration_seconds, job.duration_seconds, resolution) : 0;
  const videoUrl = job?.output_url ?? job?.preview_url;
  const previewPending = pendingAction?.type === "render" && pendingAction.resolution === "480p";
  const hdPending = pendingAction?.type === "render" && pendingAction.resolution === "1080p";
  const actionMessage = pendingAction
    ? pendingAction.type === "render"
      ? `Demande de rendu ${pendingAction.resolution} / 25 fps en cours de prise en compte…`
      : pendingAction.type === "cancel" ? "Demande d'annulation en cours de prise en compte…" : "Validation du traitement en cours de prise en compte…"
    : !error && job?.inspection_required
      ? state === "queued" ? `Demande acceptée. Le rendu ${job.resolution} est en attente du moteur Blender…`
        : state === "rendering" ? `Rendu ${job.resolution} en cours…`
          : state === "encoding" ? "Images calculées. Encodage de la vidéo en cours…" : null
      : null;
  return (
    <>
    {job?.viewport_url && <figure className="video-viewport-preview">
      <figcaption>Viewport de l'animation proposée</figcaption>
      <button type="button" className="msg-image-btn" aria-label="Agrandir le screenshot du viewport" onClick={() => setViewportOpen(true)}>
        <img className="video-poster" src={blobProxyUrl(job.viewport_url)} alt="Screenshot du viewport de l'animation proposée" onLoad={() => setViewportError(false)} onError={() => setViewportError(true)} />
      </button>
      {viewportError && <p className="media-error" role="alert">Impossible de charger le screenshot. Actualise le statut pour renouveler son lien.</p>}
    </figure>}
    <article className="video-job" aria-label={`Video job ${id}`} aria-busy={loading || acting}>
      <header className="video-job-header">
        <strong>Video job</strong>
        <span role="status">{acting ? "Prise en compte…" : state.replaceAll("_", " ")}</span>
        <button className="media-icon" type="button" title="Refresh job status" aria-label="Refresh job status" disabled={!enabled || busy || loading || acting} onClick={retry}><RefreshCw size={17} /></button>
      </header>
      <code className="video-job-id">{id}</code>
      {job && <>
        <div className="video-job-meta">{job.mode} · {job.duration_seconds}s · {job.fps} fps · {job.resolution}</div>
        {actionMessage && <div className="video-action-status" role="status" aria-live="polite"><Loader2 size={18} aria-hidden /> <span>{actionMessage}</span></div>}
        {!paused(state) && <progress aria-label="Video job progress" max={100} value={job.progress} />}
        {job.scene_url && <a className="video-download" href={blobProxyUrl(job.scene_url)} download={`devfest-${id}.blend`}><Download size={16} /> Télécharger l'animation Blender (.blend)</a>}
        {state === "awaiting_render_approval" && <fieldset className="video-approval" disabled={locked}>
          <legend>Inspecter avant de rendre</legend>
          <p>Ouvre le fichier .blend dans Blender et lance la lecture de la timeline pour vérifier les personnages, les mouvements et la caméra. Aucun rendu vidéo n'a encore démarré.</p>
          <label className="media-check"><input type="checkbox" checked={inspected} onChange={(event) => setInspected(event.target.checked)} /> J'ai inspecté l'animation proposée et je valide son rendu.</label>
          <button type="button" className="media-command" disabled={!inspected || locked} onClick={() => { void act("render"); }}>{previewPending ? <Loader2 className="video-action-spinner" size={17} aria-hidden /> : <Check size={17} />} {previewPending ? "Prise en compte…" : "Lancer le rendu 480p / 25 fps"}</button>
        </fieldset>}
        {videoUrl && <video className="inline-video" controls playsInline preload="metadata" src={blobProxyUrl(videoUrl)} poster={job.poster_url ? blobProxyUrl(job.poster_url) : undefined} />}
        {!videoUrl && job.poster_url && <img className="video-poster" src={blobProxyUrl(job.poster_url)} alt="Video preview" />}
        {videoUrl && <a className="video-download" href={blobProxyUrl(videoUrl)} download={`${id}.mp4`}><Download size={16} /> {job.inspection_required ? state === "completed" && job.resolution === "1080p" ? "Télécharger MP4 1080p" : "Télécharger aperçu MP4 480p" : "Download MP4"}</a>}
        {job.inspection_required && job.resolution === "1080p" && state === "completed" && job.preview_url && <a className="video-download" href={blobProxyUrl(job.preview_url)} download={`${id}-480p.mp4`}><Download size={16} /> Conserver l'aperçu 480p</a>}
        {job.hd_available && <fieldset className="video-approval" disabled={locked}>
          <legend>Cette vidéo te plaît ?</legend>
          <p>Le rendu natif 1080p / 25 fps utilise exactement l'animation inspectée, même si la scène du chat a changé. Il calcule environ 5 fois plus de pixels et reste limité à 60 minutes.</p>
          <label className="media-check"><input type="checkbox" checked={hdConsent} onChange={(event) => setHdConsent(event.target.checked)} /> Je valide cet aperçu et souhaite le rendu 1080p.</label>
          <button type="button" className="media-command" disabled={!hdConsent || locked} onClick={() => { void act("render", "1080p"); }}>{hdPending ? <Loader2 className="video-action-spinner" size={17} aria-hidden /> : <Check size={17} />} {hdPending ? "Prise en compte…" : "Générer en 1080p / 25 fps"}</button>
        </fieldset>}
        {job.inspection_required && job.resolution === "1080p" && !paused(state) && <p className="media-warning">Rendu 1080p en cours. L'aperçu 480p reste disponible pendant le calcul.</p>}
        {job.error && <p className="media-error" role="alert">{job.error}</p>}
        {state === "submission_unknown" && <p className="media-warning">Submission outcome is unknown. Automatic polling has stopped. Do not submit again; verify the existing external job before taking further action.</p>}
        {state === "awaiting_seedance_approval" && <fieldset className="video-approval" disabled={locked || !job.seedance_enabled}>
          <legend>Paid external processing</legend>
          {!job.seedance_enabled && <p className="media-warning">Seedance is disabled on the agent.</p>}
          <label>Prompt<textarea value={prompt} maxLength={8000} onChange={(event) => { setPrompt(event.target.value); setConsent(false); }} rows={2} /></label>
          <div className="video-options">
            <label>Resolution<select value={resolution} onChange={(event) => { setResolution(event.target.value as VideoResolution); setConsent(false); }}>{Object.keys(RESOLUTION_RATES).map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
            <label className="media-check"><input type="checkbox" checked={audio} onChange={(event) => { setAudio(event.target.checked); setConsent(false); }} /> Generate audio</label>
          </div>
          <p className="video-estimate">Estimated ${estimate.toFixed(2)} USD: ({job.duration_seconds}s input + {job.duration_seconds}s output) × ${RESOLUTION_RATES[resolution].toFixed(2)}/s.</p>
          <p className="media-warning">Assumes equal input and output duration; final provider charges may differ.</p>
          <label className="media-check"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} /> I consent to sending this video and prompt to WaveSpeed / Seedance and to the paid processing estimate.</label>
          <button type="button" className="media-command" disabled={!consent || !prompt.trim() || locked || !job.seedance_enabled} onClick={() => { void act("approve"); }}><Check size={17} /> Approve paid processing</button>
        </fieldset>}
        {!TERMINAL_JOB_STATES.has(state) && state !== "wavespeed_submitting" && <button type="button" className="media-command secondary" disabled={locked} onClick={() => { void act("cancel"); }}><X size={16} /> Cancel job</button>}
      </>}
      {!enabled && <p className="media-warning">Media controls are disabled.</p>}
      {error && <p className="media-error" role="alert">{error} <button type="button" className="media-command secondary" disabled={!enabled || busy || loading || acting} onClick={retry}><RefreshCw size={15} /> Retry status</button></p>}
    </article>
    {viewportOpen && job?.viewport_url && <ImageLightbox src={blobProxyUrl(job.viewport_url)} alt="Viewport de l'animation proposée" onClose={() => setViewportOpen(false)} />}
    </>
  );
}
