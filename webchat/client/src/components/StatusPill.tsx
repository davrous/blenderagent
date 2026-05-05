interface Props {
  status: string;
}

export function StatusPill({ status }: Props) {
  return (
    <div className="status-pill" role="status">
      <span className="status-pill-dot" aria-hidden />
      <span>{status}</span>
    </div>
  );
}
