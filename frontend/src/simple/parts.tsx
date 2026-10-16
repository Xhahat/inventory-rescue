import { Icon } from './icons';
import type { IconName } from './icons';

export function Back() {
  return (
    <a className="s-back" href="#/">
      <Icon name="back" size={20} /> Back
    </a>
  );
}

/** Title band at the top of the Make my plan, Read a message and Ask a question screens. */
export function Hero({ icon, title, sub }: { icon: IconName; title: string; sub: string }) {
  return (
    <header className="s-hero">
      <Back />
      <div className="s-hero-row">
        <span className="s-hero-icon" aria-hidden>
          <Icon name={icon} size={28} />
        </span>
        <h1>{title}</h1>
      </div>
      <p className="s-hero-sub">{sub}</p>
      <svg className="s-hero-art" viewBox="0 0 220 160" aria-hidden>
        <g fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round">
          <path d="M110 18 170 50 170 112 110 144 50 112 50 50Z" />
          <path d="M50 50 110 82 170 50M110 82V144" />
          <path d="M80 34 140 66M80 66 140 98" opacity=".5" />
          <path d="M12 132h40M168 24h40M190 100h22" strokeDasharray="4 5" opacity=".6" />
        </g>
      </svg>
    </header>
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
