interface Props {
  status: string;
}

export function StatusPill({ status }: Props) {
  const music = status.startsWith("🎵");
  return (
    <div className={`status-pill${music ? " music-analysis" : ""}`} role="status">
      {music ? <span className="music-notes" aria-hidden><span>♪</span><span>♫</span><span>♪</span></span> : <span className="status-pill-dot" aria-hidden />}
      <span>{status}</span>
    </div>
  );
}
