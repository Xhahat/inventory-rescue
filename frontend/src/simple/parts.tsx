export function Back() {
  return (
    <a className="s-back" href="#/">
      ← Back
    </a>
  );
}

export function Working({ text }: { text: string }) {
  return (
    <div className="s-working" role="status">
      <span className="s-dot" /> {text}
    </div>
  );
}

export function Problem({ error }: { error: string | null }) {
  if (!error) return null;
  return (
    <div className="s-problem" role="alert">
      Something went wrong. Please try again.
      <div className="s-problem-detail">{error}</div>
    </div>
  );
}
