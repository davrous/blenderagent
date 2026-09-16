import { useEffect, useRef, useState } from "react";
import { Check, Download, RefreshCw, X } from "lucide-react";
import { useChatStore } from "../state/chatStore";
import { blobProxyUrl, paidEstimate, requestVideoJob, RESOLUTION_RATES, TERMINAL_JOB_STATES, type VideoJob, type VideoResolution } from "../api/media";

export function VideoJobCard({ id, conversationId, enabled }: { id: string; conversationId: string; enabled: boolean }) {
  const busy = useChatStore((state) => state.isStreaming || state.voiceActive);
  const [job, setJob] = useState<VideoJob | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [acting, setActing] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [prompt, setPrompt] = useState("");
  const [resolution, setResolution] = useState<VideoResolution>("720p");
  const [audio, setAudio] = useState(false);
  const [consent, setConsent] = useState(false);
  const stopped = useRef(false);

  const retry = () => { stopped.current = false; setError(""); setConsent(false); setRefresh((value) => value + 1); };

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
        stopped.current = TERMINAL_JOB_STATES.has(latest.state);
        if (!TERMINAL_JOB_STATES.has(latest.state)) timer = setTimeout(() => { void poll(); }, 7500);
      } catch (failure) {
        if (!disposed) { stopped.current = true; setError(failure instanceof Error ? failure.message : "Status check failed"); }
      } finally { if (!disposed) setLoading(false); }
    };
    void poll();
    return () => { disposed = true; clearTimeout(timer); };
  }, [conversationId, id, enabled, busy, acting, refresh]);

  const act = async (action: "approve" | "cancel") => {
    const current = useChatStore.getState();
    if (!enabled || loading || acting || current.isStreaming || current.voiceActive || error || !job) return;
    if (action === "approve" && (!consent || !job.seedance_enabled || job.state !== "awaiting_seedance_approval")) return;
    setActing(true);
    setConsent(false);
    try {
      const latest = await requestVideoJob(conversationId, id, action, action === "approve" ? { prompt, resolution, generate_audio: audio } : undefined);
      stopped.current = TERMINAL_JOB_STATES.has(latest.state);
      setJob(latest);
    } catch (failure) {
      stopped.current = true;
      setError(`${failure instanceof Error ? failure.message : "Action failed"} Refresh status to check the outcome; this action was not retried.`);
    } finally { setLoading(false); setActing(false); }
  };

  const locked = busy || loading || acting || !enabled || !!error;
  const state = job?.state ?? "loading";
  const estimate = job ? paidEstimate(job.duration_seconds, job.duration_seconds, resolution) : 0;
  const videoUrl = job?.output_url ?? job?.preview_url;
  return (
    <article className="video-job" aria-label={`Video job ${id}`} aria-busy={loading || acting}>
      <header className="video-job-header">
        <strong>Video job</strong>
        <span role="status">{acting ? "Submitting action..." : state.replaceAll("_", " ")}</span>
        <button className="media-icon" type="button" title="Refresh job status" aria-label="Refresh job status" disabled={!enabled || busy || loading || acting} onClick={retry}><RefreshCw size={17} /></button>
      </header>
      <code className="video-job-id">{id}</code>
      {job && <>
        <div className="video-job-meta">{job.mode} · {job.duration_seconds}s · {job.fps} fps · {job.resolution}</div>
        {!TERMINAL_JOB_STATES.has(state) && <progress aria-label="Video job progress" max={100} value={job.progress} />}
        {videoUrl && <video className="inline-video" controls playsInline preload="metadata" src={blobProxyUrl(videoUrl)} poster={job.poster_url ? blobProxyUrl(job.poster_url) : undefined} />}
        {!videoUrl && job.poster_url && <img className="video-poster" src={blobProxyUrl(job.poster_url)} alt="Video preview" />}
        {videoUrl && <a className="video-download" href={blobProxyUrl(videoUrl)} download={`${id}.mp4`}><Download size={16} /> Download MP4</a>}
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
  );
}
