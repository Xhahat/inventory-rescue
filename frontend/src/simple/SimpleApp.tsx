import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Overview } from '../api';
import Curtain from '../landing/Curtain';
import Landing from '../landing/Landing';
import AskScreen from './AskScreen';
import MessageScreen from './MessageScreen';
import PlanScreen from './PlanScreen';
import './simple.css';
import '../landing/skin.css';

function Home({ offline }: { offline: boolean }) {
  const [ov, setOv] = useState<Overview | null>(null);
  useEffect(() => {
    api.overview().then(setOv).catch(() => setOv(null));
  }, []);
  const atRisk = ov ? new Set(ov.alerts.map((a) => a.entity_id)).size : 0;

  return <Landing ov={ov} atRisk={atRisk} offline={offline} />;
}

export default function SimpleApp({ screen }: { screen: string }) {
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    api.health().catch(() => setOffline(true));
  }, []);

  // Page change: a curtain covers the page, the screen swaps underneath, then the curtain lifts.
  const [reduceMotion] = useState(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  const [lagged, setShown] = useState(screen);
  const shown = reduceMotion ? screen : lagged;
  const [sweeps, setSweeps] = useState(0);
  useEffect(() => {
    if (reduceMotion || screen === lagged) return;
    setSweeps((n) => n + 1);
    const t = window.setTimeout(() => {
      setShown(screen);
      window.scrollTo(0, 0);
    }, 600);
    return () => window.clearTimeout(t);
  }, [screen, lagged, reduceMotion]);

  // Reduced motion skips the curtain, so reset the scroll position when the screen changes.
  useEffect(() => {
    if (reduceMotion) window.scrollTo(0, 0);
  }, [screen, reduceMotion]);

  const inner = ['plan', 'message', 'ask'].includes(shown);

  return (
    <div className={inner ? 'simple simple-inner' : 'simple'}>
      <header className="s-top">
        <a className="s-brand" href="#/">
          <span className="s-mark" aria-hidden>IR</span> Inventory Rescue
        </a>
        <a className="s-advanced" href="#/plan" title="Charts, tables and tests for experienced planners">
          Advanced view
        </a>
      </header>
      <main className={inner ? 's-main' : 's-main s-main-wide'}>
        <div key={shown} className={inner ? 'lp-page-in' : undefined}>
          {shown === 'plan' ? (
            <PlanScreen />
          ) : shown === 'message' ? (
            <MessageScreen />
          ) : shown === 'ask' ? (
            <AskScreen />
          ) : (
            <Home offline={offline} />
          )}
        </div>
      </main>
      <footer className="s-foot">Nothing is ever sent to your suppliers without you.</footer>
      {sweeps > 0 && <Curtain key={sweeps} />}
    </div>
  );
}
