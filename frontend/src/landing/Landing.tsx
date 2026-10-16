import { Fragment, useEffect, useRef, useState } from 'react';
import type { CSSProperties, PointerEvent as RPointerEvent, ReactNode } from 'react';
import type { Overview } from '../api';
import { Icon } from '../simple/icons';
import type { IconName } from '../simple/icons';
import { day } from '../simple/plain';
import Intro from './Intro';
import { CHAPTERS, STOPS, TOTAL_LOAD, loadedCount } from './timeline';
import type { AnchorId, HoverInfo, WarehouseScene } from './warehouse';
import './landing.css';

// The intro plays once per page load, not every time the user comes back to the start page.
let introPlayed = false;

const clamp01 = (v: number) => Math.min(1, Math.max(0, v));
const sstep = (a: number, b: number, v: number) => {
  const t = clamp01((v - a) / (b - a));
  return t * t * (3 - 2 * t);
};
const vars = (v: Record<string, string | number>) => v as CSSProperties;

const RAIL = ['Start', 'See', 'Spot', 'Plan', 'Deliver'];
const PAGE_LABELS = ['Start', 'See', 'Spot', 'Plan', 'Deliver'];
const TIP: Record<NonNullable<HoverInfo>['kind'], string> = {
  risk: 'Running low. Needs attention.',
  fixed: 'Covered by the plan.',
  ok: 'In stock.',
};

// Labels pinned to objects in the 3D scene: [fade in start, in end, out start, out end].
const TAGS: { id: AnchorId; text: string; color: string; win: [number, number, number, number]; later?: [number, string] }[] = [
  { id: 'drone', text: 'Drone scanning shelves', color: '#2563eb', win: [-1, 0, 0.27, 0.32] },
  { id: 'forklift', text: 'Forklift restocking', color: '#2563eb', win: [-1, 0, 0.27, 0.32] },
  { id: 'risk', text: 'Running low', color: '#f59e0b', win: [0.37, 0.41, 0.5, 0.53] },
  { id: 'scan', text: 'Scan gate counting boxes', color: '#2563eb', win: [0.57, 0.61, 0.77, 0.8] },
  { id: 'truck', text: 'Truck loading', color: '#2563eb', win: [0.6, 0.64, 0.96, 0.99], later: [0.85, 'On its way'] },
];

function Words({ text }: { text: string }) {
  return (
    <>
      {text.split(' ').map((w, i) => (
        <Fragment key={i}>
          <span className="lp-w">
            <span style={vars({ '--i': i })}>{w}</span>
          </span>{' '}
        </Fragment>
      ))}
    </>
  );
}

function CountUp({ to, reduce }: { to: number; reduce: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (reduce) {
      el.textContent = String(to);
      return;
    }
    let raf = 0;
    const io = new IntersectionObserver(
      ([e]) => {
        if (!e.isIntersecting) return;
        io.disconnect();
        const t0 = performance.now();
        const step = (now: number) => {
          const k = clamp01((now - t0) / 1400);
          el.textContent = String(Math.round(to * (1 - Math.pow(1 - k, 3))));
          if (k < 1) raf = requestAnimationFrame(step);
        };
        raf = requestAnimationFrame(step);
      },
      { threshold: 0.6 },
    );
    io.observe(el);
    return () => {
      io.disconnect();
      cancelAnimationFrame(raf);
    };
  }, [to, reduce]);
  return <span ref={ref}>0</span>;
}

function Tile({ href, icon, title, text, badge, kind }: {
  href: string; icon: IconName; title: string; text: string; badge?: number; kind: 'plan' | 'msg' | 'ask';
}) {
  const onMove = (e: RPointerEvent<HTMLAnchorElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width - 0.5;
    const y = (e.clientY - r.top) / r.height - 0.5;
    const s = e.currentTarget.style;
    s.setProperty('--rx', `${(-y * 6).toFixed(2)}deg`);
    s.setProperty('--ry', `${(x * 8).toFixed(2)}deg`);
    s.setProperty('--mx', `${(x + 0.5) * 100}%`);
    s.setProperty('--my', `${(y + 0.5) * 100}%`);
  };
  const onLeave = (e: RPointerEvent<HTMLAnchorElement>) => {
    e.currentTarget.style.setProperty('--rx', '0deg');
    e.currentTarget.style.setProperty('--ry', '0deg');
  };
  return (
    <a className={`lp-tile lp-tile-${kind}`} href={href} onPointerMove={onMove} onPointerLeave={onLeave}>
      <span className="lp-tile-icon">
        <Icon name={icon} size={28} />
      </span>
      <span className="lp-tile-body">
        <span className="lp-tile-title">
          {title}
          {!!badge && <span className="lp-count">{badge} new</span>}
        </span>
        <span className="lp-tile-text">{text}</span>
      </span>
      <span className="lp-tile-go">
        <Icon name="arrow" size={22} />
      </span>
    </a>
  );
}

function Chapter({ index, label, children, setRef }: {
  index: number; label?: string; children: ReactNode; setRef: (i: number, el: HTMLDivElement | null) => void;
}) {
  return (
    <div className="lp-chapter" ref={(el) => setRef(index, el)}>
      {label && (
        <div className="lp-card">
          <p className="lp-step">
            <b>0{index}</b>
            <i />
            {label}
          </p>
          {children}
        </div>
      )}
      {!label && children}
    </div>
  );
}

export default function Landing({ ov, atRisk, offline }: { ov: Overview | null; atRisk: number; offline: boolean }) {
  const [reduce] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [intro, setIntro] = useState(!introPlayed && !reduce);
  const [go, setGo] = useState(!intro);
  const [sceneReady, setSceneReady] = useState(false);
  const [noGl, setNoGl] = useState(false);
  const [timedOut, setTimedOut] = useState(false);
  const [hover, setHover] = useState<HoverInfo>(null);

  const storyRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<WarehouseScene | null>(null);
  const chapterEls = useRef<(HTMLDivElement | null)[]>([]);
  const dotEls = useRef<(HTMLButtonElement | null)[]>([]);
  const tagEls = useRef<(HTMLDivElement | null)[]>([]);
  const loadFill = useRef<HTMLSpanElement>(null);
  const loadPct = useRef<HTMLSpanElement>(null);
  const cueRef = useRef<HTMLDivElement>(null);
  const fadeRef = useRef<HTMLDivElement>(null);
  const barRef = useRef<HTMLSpanElement>(null);
  const vigRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLElement>(null);
  const numRef = useRef<HTMLElement>(null);
  const lblRef = useRef<HTMLSpanElement>(null);
  const pager = useRef<{ go: (page: number) => void }>({ go: () => {} });
  const introOn = useRef(intro);
  const atRiskRef = useRef(atRisk);
  useEffect(() => {
    introOn.current = intro;
    atRiskRef.current = atRisk;
  });

  const setRef = (i: number, el: HTMLDivElement | null) => {
    chapterEls.current[i] = el;
  };

  useEffect(() => {
    const t = window.setTimeout(() => setTimedOut(true), 1500);
    return () => window.clearTimeout(t);
  }, []);
  const settled = ov !== null || offline || timedOut;

  // Build the 3D scene lazily so the rest of the app never downloads three.js.
  useEffect(() => {
    if (!settled) return;
    let dead = false;
    let scene: WarehouseScene | null = null;
    import('./warehouse')
      .then((m) => {
        if (dead || !hostRef.current) return;
        try {
          scene = m.createWarehouse({ host: hostRef.current, atRisk: atRiskRef.current, reduceMotion: reduce, onHover: setHover });
          sceneRef.current = scene;
        } catch {
          setNoGl(true);
        }
        setSceneReady(true);
      })
      .catch(() => {
        setNoGl(true);
        setSceneReady(true);
      });
    return () => {
      dead = true;
      scene?.dispose();
      sceneRef.current = null;
    };
  }, [settled, reduce]);

  // The truck drives in once the curtains open and the scene exists.
  useEffect(() => {
    if (go && sceneReady) sceneRef.current?.arrive();
  }, [go, sceneReady]);

  // Scroll -> smoothed progress -> chapters, labels, HUD and scene.
  useEffect(() => {
    let raf = 0;
    let last = performance.now();
    const t0 = last;
    let cur = 0;
    const tick = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000);
      last = now;
      const story = storyRef.current;
      if (story) {
        const r = story.getBoundingClientRect();
        const vh = window.innerHeight;
        const target = clamp01(-r.top / Math.max(1, r.height - vh));
        cur = reduce ? target : cur + (target - cur) * (1 - Math.exp(-dt * 7));
        if (Math.abs(target - cur) < 0.0002) cur = target;
        if (r.bottom > 0 && r.top < vh) {
          story.style.setProperty('--hdr', `${Math.max(0, r.top).toFixed(1)}px`);
          CHAPTERS.forEach(([a, b, c, d], i) => {
            const el = chapterEls.current[i];
            if (!el) return;
            const vin = sstep(a, b, cur);
            const vout = 1 - sstep(c, d, cur);
            const o = Math.min(vin, vout);
            el.style.opacity = String(o);
            el.style.visibility = o < 0.01 ? 'hidden' : 'visible';
            const y = (1 - vin) * 54 - (1 - vout) * 54;
            const s = 0.97 + 0.03 * o;
            el.style.transform = `translate3d(0, ${y.toFixed(1)}px, 0) scale(${s.toFixed(4)})`;
          });
          const idx = cur < 0.16 ? 0 : cur < 0.34 ? 1 : cur < 0.53 ? 2 : cur < 0.82 ? 3 : 4;
          dotEls.current.forEach((d, i) => d?.classList.toggle('on', i === idx));
          if (numRef.current && numRef.current.dataset.i !== String(idx)) {
            numRef.current.dataset.i = String(idx);
            numRef.current.textContent = `0${idx + 1}`;
            if (lblRef.current) lblRef.current.textContent = PAGE_LABELS[idx];
          }
          const pct = Math.round((loadedCount(cur) / TOTAL_LOAD) * 100);
          if (loadPct.current) loadPct.current.textContent = String(pct);
          if (loadFill.current) loadFill.current.style.transform = `scaleX(${pct / 100})`;
          if (barRef.current) barRef.current.style.transform = `scaleY(${cur.toFixed(4)})`;
          if (cueRef.current) cueRef.current.style.opacity = String(1 - sstep(0.02, 0.08, cur));
          if (fadeRef.current) fadeRef.current.style.opacity = String(sstep(0.94, 1, cur));
          if (vigRef.current) vigRef.current.style.opacity = String(0.3 + 0.7 * (1 - sstep(0.1, 0.18, cur)));
          const scene = sceneRef.current;
          if (scene) {
            scene.render(cur, (now - t0) / 1000);
            TAGS.forEach((tg, i) => {
              const el = tagEls.current[i];
              if (!el) return;
              const [a, b, c, d] = tg.win;
              const o = Math.min(sstep(a, b, cur), 1 - sstep(c, d, cur));
              const pt = scene.project(tg.id);
              el.style.opacity = pt.visible ? String(o) : '0';
              el.style.transform = `translate3d(${pt.x.toFixed(1)}px, ${pt.y.toFixed(1)}px, 0)`;
              if (tg.later) {
                const txt = cur >= tg.later[0] ? tg.later[1] : tg.text;
                const label = el.querySelector('.lp-tag-in > span');
                if (label && label.textContent !== txt) label.textContent = txt;
              }
            });
          }
        }
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [reduce]);

  // One wheel step, swipe or key press moves exactly one page: Start, See, Spot, Plan, Deliver, then the menu.
  useEffect(() => {
    const story = storyRef.current;
    const end = endRef.current;
    if (!story || !end) return;
    let raf = 0;
    let animTo: number | null = null;
    let lastEvt = 0;
    let animEnd = 0;

    const positions = () => {
      const top0 = story.getBoundingClientRect().top + window.scrollY;
      const span = story.offsetHeight - window.innerHeight;
      return [0, ...STOPS.map((p) => top0 + p * span), end.getBoundingClientRect().top + window.scrollY];
    };
    const stepTo = (y: number) => {
      cancelAnimationFrame(raf);
      const from = window.scrollY;
      const dist = y - from;
      if (reduce || Math.abs(dist) < 2) {
        window.scrollTo(0, y);
        animTo = null;
        return;
      }
      const dur = Math.min(1500, 950 + (Math.abs(dist) / window.innerHeight) * 160);
      const t0 = performance.now();
      animTo = y;
      const run = (now: number) => {
        const k = clamp01((now - t0) / dur);
        const e = k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2;
        window.scrollTo(0, from + dist * e);
        if (k < 1) raf = requestAnimationFrame(run);
        else {
          animTo = null;
          animEnd = performance.now();
        }
      };
      raf = requestAnimationFrame(run);
    };
    // The next page in a direction, or null when normal scrolling should take over (inside the menu, or already at the top).
    const nextPage = (dir: 1 | -1): number | null => {
      const pos = positions();
      const endTop = pos[pos.length - 1];
      const y = animTo ?? window.scrollY;
      if (dir > 0) return y >= endTop - 4 ? null : (pos.find((v) => v > y + 8) ?? null);
      if (y > endTop + 30) return null;
      const base = y > endTop - 8 ? endTop : y;
      for (let i = pos.length - 1; i >= 0; i--) if (pos[i] < base - 8) return pos[i];
      return null;
    };
    const handle = (dir: 1 | -1, e: Event) => {
      if (introOn.current) return;
      const target = nextPage(dir);
      if (target === null && animTo === null) return;
      e.preventDefault();
      const now = performance.now();
      const gap = now - lastEvt;
      lastEvt = now;
      // ignore the rest of a trackpad swipe or a held key until the input pauses
      if (animTo !== null || gap < 220 || now - animEnd < 250 || target === null) return;
      stepTo(target);
    };

    const onWheel = (e: WheelEvent) => {
      if (e.ctrlKey || Math.abs(e.deltaY) < 3 || Math.abs(e.deltaX) > Math.abs(e.deltaY)) return;
      handle(e.deltaY > 0 ? 1 : -1, e);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (e.key === ' ' && t && /^(BUTTON|A)$/.test(t.tagName)) return;
      if (e.key === 'ArrowDown' || e.key === 'PageDown' || e.key === ' ') handle(1, e);
      else if (e.key === 'ArrowUp' || e.key === 'PageUp') handle(-1, e);
    };
    let ty0 = 0;
    let swiped = false;
    const onTouchStart = (e: TouchEvent) => {
      ty0 = e.touches[0].clientY;
      swiped = false;
    };
    const onTouchMove = (e: TouchEvent) => {
      const dy = ty0 - e.touches[0].clientY;
      if (Math.abs(dy) < 2 || introOn.current) return;
      const dir = dy > 0 ? 1 : -1;
      const target = nextPage(dir);
      if (target === null && animTo === null) return;
      if (e.cancelable) e.preventDefault();
      if (swiped || animTo !== null || Math.abs(dy) < 36 || target === null) return;
      swiped = true;
      stepTo(target);
    };

    pager.current.go = (page) => {
      const pos = positions();
      stepTo(pos[Math.max(0, Math.min(pos.length - 1, page))]);
    };
    window.addEventListener('wheel', onWheel, { passive: false });
    window.addEventListener('keydown', onKey);
    window.addEventListener('touchstart', onTouchStart, { passive: true });
    window.addEventListener('touchmove', onTouchMove, { passive: false });
    return () => {
      cancelAnimationFrame(raf);
      window.removeEventListener('wheel', onWheel);
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('touchstart', onTouchStart);
      window.removeEventListener('touchmove', onTouchMove);
    };
  }, [reduce]);

  // Reveal-on-scroll for the closing section.
  useEffect(() => {
    const els = document.querySelectorAll('.lp-reveal');
    const io = new IntersectionObserver(
      (list) => list.forEach((e) => e.isIntersecting && (e.target.classList.add('lp-in'), io.unobserve(e.target))),
      { threshold: 0.15 },
    );
    els.forEach((el) => io.observe(el));
    return () => io.disconnect();
  }, [ov, offline]);

  const products = ov?.n_skus ?? 0;
  const statusText = atRisk
    ? `${atRisk} product${atRisk > 1 ? 's need' : ' needs'} attention. A plan will fix this.`
    : 'All products look fine.';
  const status = ov && (
    <p className={`lp-status ${atRisk ? 'lp-status-warn' : 'lp-status-ok'}`}>
      <span className="lp-status-dot" />
      {statusText}
    </p>
  );

  return (
    <div className={`lp${go ? ' lp-go' : ''}`}>
      {intro && (
        <Intro
          ready={sceneReady}
          onReveal={() => setGo(true)}
          onDone={() => {
            introPlayed = true;
            setIntro(false);
          }}
        />
      )}

      <div className="lp-story" ref={storyRef}>
        <div className="lp-sticky">
          <div className="lp-canvas" ref={hostRef} />
          {noGl && (
            <div className="lp-nogl" aria-hidden>
              <Icon name="truck" size={96} />
            </div>
          )}
          <div className="lp-vignette" ref={vigRef} aria-hidden />

          <div className="lp-tags" aria-hidden>
            {TAGS.map((tg, i) => (
              <div className="lp-tag" key={tg.id} ref={(el) => { tagEls.current[i] = el; }}>
                <div className="lp-tag-in" style={vars({ '--c': tg.color })}>
                  <i className="lp-tag-dot" />
                  <span>{tg.text}</span>
                </div>
              </div>
            ))}
          </div>

          <div className="lp-layer">
            <Chapter index={0} setRef={setRef}>
              <p className="lp-eyebrow">
                <i className="lp-live" />
                {ov ? `Stock as of ${day(ov.as_of)}` : 'Inventory, made simple'}
              </p>
              <h1 className="lp-h1">
                <Words text="Rescue your stock" />
                <br />
                <span className="lp-h1-accent">
                  <Words text="before it runs out." />
                </span>
              </h1>
              <p className="lp-lead">
                See what is at risk, get a plan you can approve in one click, and keep every box moving. Written in plain words.
              </p>
              {offline && <div className="lp-problem">We can't reach the server right now. Please try again in a minute.</div>}
              {status}
              <div className="lp-actions">
                <a className="lp-btn lp-btn-primary" href="#/s/plan">
                  Make my plan <Icon name="arrow" size={20} />
                </a>
                <button type="button" className="lp-btn lp-btn-ghost" onClick={() => pager.current.go(1)}>
                  See how it works
                </button>
              </div>
            </Chapter>

            <Chapter index={1} label="See" setRef={setRef}>
              <h2 className="lp-h2">Every box, in plain sight.</h2>
              <p className="lp-body">
                Stock, orders and deliveries in one calm picture. No spreadsheets. No jargon. Just what you have and what is coming.
              </p>
              <ul className="lp-chips">
                {products > 0 && <li>{products} products tracked</li>}
                <li>Looks 30 days ahead</li>
                <li>Plain words</li>
              </ul>
            </Chapter>

            <Chapter index={2} label="Spot" setRef={setRef}>
              <h2 className="lp-h2">Trouble shows up early.</h2>
              <p className="lp-body">
                {ov
                  ? atRisk
                    ? `${atRisk} product${atRisk > 1 ? 's need' : ' needs'} attention right now. The glowing boxes are the ones about to run short.`
                    : 'Everything looks healthy right now. When a product is about to run short, its box lights up here.'
                  : 'When a product is about to run short, its box lights up here.'}
              </p>
              <ul className="lp-chips">
                {!!ov?.pending_signals && <li>{ov.pending_signals} new messages to read</li>}
                <li>Try hovering a box</li>
              </ul>
            </Chapter>

            <Chapter index={3} label="Plan" setRef={setRef}>
              <h2 className="lp-h2">Approve the plan. Boxes start moving.</h2>
              <p className="lp-body">
                Tell us your budget. We say what to buy and what to put on sale, then line up the shipment.
              </p>
              <div className="lp-load" aria-hidden>
                <div className="lp-load-top">
                  <span>Loading the truck</span>
                  <span>
                    <span ref={loadPct}>0</span>%
                  </span>
                </div>
                <div className="lp-load-bar">
                  <span ref={loadFill} />
                </div>
              </div>
            </Chapter>

            <Chapter index={4} label="Deliver" setRef={setRef}>
              <h2 className="lp-h2">Loaded. Off it goes.</h2>
              <p className="lp-body">Nothing is ever sent to your suppliers without you. You stay in control.</p>
            </Chapter>
          </div>

          <nav className="lp-rail" aria-label="Story progress">
            <span className="lp-rail-track" aria-hidden>
              <span ref={barRef} />
            </span>
            {RAIL.map((label, i) => (
              <button
                key={label}
                type="button"
                ref={(el) => {
                  dotEls.current[i] = el;
                }}
                className={i === 0 ? 'on' : ''}
                onClick={() => pager.current.go(i)}
              >
                <span>{label}</span>
                <i />
              </button>
            ))}
          </nav>

          <div className="lp-pg" aria-hidden>
            <b ref={numRef} data-i="0">01</b>
            <i />
            <span>05</span>
            <em ref={lblRef}>Start</em>
          </div>
          <div className="lp-cue" ref={cueRef} aria-hidden>
            <span className="lp-mouse">
              <i />
            </span>
            Scroll
          </div>
          <div className="lp-fade" ref={fadeRef} aria-hidden />
        </div>
      </div>

      {hover && (
        <div className={`lp-tip lp-tip-${hover.kind}`} style={{ left: hover.x + 16, top: hover.y + 16 }}>
          {TIP[hover.kind]}
        </div>
      )}

      <section className="lp-end" ref={endRef} aria-labelledby="lp-end-title">
        <div className="lp-blobs" aria-hidden>
          <i /><i /><i /><i />
        </div>
        <div className="lp-marquee" aria-hidden>
          <div className="lp-marquee-track">
            {Array.from({ length: 2 }).map((_, k) => (
              <span key={k}>
                {['Forecast', 'Reorder', 'Put on sale', 'Fast delivery', 'Read messages', 'Ask anything'].map((w) => (
                  <em key={w}>
                    {w}
                    <b />
                  </em>
                ))}
              </span>
            ))}
          </div>
        </div>
        <div className="lp-end-inner">
          {ov && (
            <p className="lp-eyebrow lp-reveal">
              <i className="lp-live" />
              Stock as of {day(ov.as_of)}
            </p>
          )}
          <h2 id="lp-end-title" className="lp-h1 lp-h1-end lp-reveal">
            What would you like <span className="lp-h1-accent">to do?</span>
          </h2>
          {offline && <div className="lp-problem lp-reveal">We can't reach the server right now. Please try again in a minute.</div>}
          {ov && <div className="lp-reveal">{status}</div>}

          {ov && (
            <ul className="lp-stats lp-reveal">
              <li className="lp-stat lp-stat-indigo">
                <strong><CountUp to={ov.n_skus} reduce={reduce} /></strong>
                <span>Products tracked</span>
              </li>
              <li className={`lp-stat ${atRisk ? 'lp-stat-coral' : 'lp-stat-mint'}`}>
                <strong><CountUp to={atRisk} reduce={reduce} /></strong>
                <span>Need attention</span>
              </li>
              <li className="lp-stat lp-stat-sky">
                <strong><CountUp to={ov.pending_signals} reduce={reduce} /></strong>
                <span>New messages</span>
              </li>
              <li className="lp-stat lp-stat-violet">
                <strong><CountUp to={ov.forecasts.fresh} reduce={reduce} /></strong>
                <span>Fresh forecasts</span>
              </li>
            </ul>
          )}

          <nav className="lp-tiles">
            <div className="lp-reveal lp-span" style={vars({ '--d': '0.05s' })}>
              <Tile kind="plan" href="#/s/plan" icon="cart" title="Make my plan"
                    text="Tell us your budget. We'll say what to buy and what to put on sale." />
            </div>
            <div className="lp-reveal" style={vars({ '--d': '0.15s' })}>
              <Tile kind="msg" href="#/s/message" icon="mail" title="Read a message" badge={ov?.pending_signals}
                    text="Paste a note from a supplier or your sales team." />
            </div>
            <div className="lp-reveal" style={vars({ '--d': '0.25s' })}>
              <Tile kind="ask" href="#/s/ask" icon="chat" title="Ask a question" text="Ask in your own words, like “What will run out first?”" />
            </div>
          </nav>
        </div>
      </section>
    </div>
  );
}
