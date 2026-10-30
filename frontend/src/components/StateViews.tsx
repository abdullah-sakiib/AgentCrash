export function Loading({ what }: { what: string }) {
  return (
    <div className="panel" role="status" aria-live="polite">
      Loading {what}…
    </div>
  );
}

export function Empty({ message }: { message: string }) {
  return <div className="panel muted">{message}</div>;
}

export function LoadError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="panel" role="alert">
      <p>
        <strong>Could not load data.</strong> {message}
      </p>
      <button type="button" className="theme-toggle" onClick={onRetry}>
        Retry
      </button>
    </div>
  );
}
