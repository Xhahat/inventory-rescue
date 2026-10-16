import { useEffect, useRef, useState } from 'react';

const MIN_MS = 2600;
const LEAVE_MS = 1100;
const FACES = ['front', 'back', 'right', 'left', 'top', 'bottom'] as const;
const WORD = 'INVENTORY RESCUE'.split('');

/** Full-screen loader: a cardboard box assembles, then the curtains split open on the page. */
export default function Intro({ ready, onReveal, onDone }: { ready: boolean; onReveal: () => void; onDone: () => void }) {
  const [phase, setPhase] = useState<'run' | 'leave'>('run');
  const pctRef = useRef<HTMLSpanElement>(null);
  const fillRef = useRef<HTMLSpanElement>(null);
  const readyRef = useRef(ready);
  const cb = useRef({ onReveal, onDone });
  useEffect(() => {
    readyRef.current = ready;
    cb.current = { onReveal, onDone };
  });

  useEffect(() => {
    const prev = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    window.scrollTo(0, 0);
    const t0 = performance.now();
    let raf = 0;
    let left = false;
    const tick = (now: number) => {
      const k = Math.min(1, (now - t0) / MIN_MS);
      const eased = 1 - Math.pow(1 - k, 2.4);
      // hold at 92% until the 3D scene has actually loaded
      const pct = Math.round(Math.min(eased * 100, readyRef.current || now - t0 > 7000 ? 100 : 92));
      if (pctRef.current) pctRef.current.textContent = String(pct);
      if (fillRef.current) fillRef.current.style.transform = `scaleX(${pct / 100})`;
      if (pct >= 100 && !left) {
        left = true;
        setPhase('leave');
        cb.current.onReveal();
        window.setTimeout(() => cb.current.onDone(), LEAVE_MS);
        return;
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(raf);
      document.body.style.overflow = prev;
    };
  }, []);

  return (
    <div className={`lp-intro lp-intro-${phase}`} role="status" aria-label="Loading Inventory Rescue">
      <div className="lp-panel lp-panel-top" />
      <div className="lp-panel lp-panel-bot" />
      <div className="lp-intro-blobs" aria-hidden>
        <i /><i /><i />
      </div>
      <div className="lp-intro-grid" aria-hidden />
      <div className="lp-intro-center">
        <div className="lp-cube-wrap" aria-hidden>
          <div className="lp-cube-shadow" />
          <div className="lp-cube">
            {FACES.map((f, i) => (
              <i key={f} className={`lp-face lp-face-${f}`} style={{ ['--d' as string]: `${i * 0.12}s` }}>
                <b />
              </i>
            ))}
          </div>
          <span className="lp-ring lp-ring-a" />
          <span className="lp-ring lp-ring-b" />
        </div>
        <div className="lp-intro-word" aria-hidden>
          {WORD.map((c, i) => (
            <span key={i} style={{ ['--i' as string]: i }}>
              {c === ' ' ? '\u00a0' : c}
            </span>
          ))}
        </div>
        <p className="lp-intro-sub">Loading the warehouse</p>
        <div className="lp-intro-bar" aria-hidden>
          <span ref={fillRef} className="lp-intro-fill" />
        </div>
        <p className="lp-intro-pct" aria-hidden>
          <span ref={pctRef}>0</span>%
        </p>
      </div>
    </div>
  );
}
