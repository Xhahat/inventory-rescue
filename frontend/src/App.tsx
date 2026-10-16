import { useEffect, useState } from 'react';
import { api } from './api';
import type { Health } from './api';
import PlanPage from './pages/PlanPage';
import InboxPage from './pages/InboxPage';
import SkuPage from './pages/SkuPage';
import ScenarioPage from './pages/ScenarioPage';
import AssistantPage from './pages/AssistantPage';
import ActivityPage from './pages/ActivityPage';
import SimpleApp from './simple/SimpleApp';

function readRoute(): string[] {
  return (window.location.hash.replace(/^#\/?/, '') || 'home').split('/');
}

function useHashRoute(): string[] {
  const [route, setRoute] = useState<string[]>(readRoute);
  useEffect(() => {
    const on = () => setRoute(readRoute());
    window.addEventListener('hashchange', on);
    return () => window.removeEventListener('hashchange', on);
  }, []);
  return route;
}

const NAV: [string, string][] = [
  ['plan', 'Recovery Plan'],
  ['inbox', 'Signal Inbox'],
  ['sku', 'SKU Detail'],
  ['scenario', 'Scenario & Value'],
  ['assistant', 'Assistant'],
  ['activity', 'Activity'],
];

export default function App() {
  const route = useHashRoute();
  if (route[0] === 'home' || route[0] === 's') return <SimpleApp screen={route[0] === 's' ? (route[1] ?? '') : ''} />;
  return <AdvancedApp route={route} />;
}

function AdvancedApp({ route }: { route: string[] }) {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthErr, setHealthErr] = useState(false);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealthErr(true));
  }, []);

  const page = route[0];
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">⟳</span> Inventory Rescue
        </div>
        <nav>
          {NAV.map(([k, label]) => (
            <a key={k} href={`#/${k}`} className={page === k ? 'active' : ''}>
              {label}
            </a>
          ))}
        </nav>
        <div className="status">
          <a className="simple-link" href="#/">
            Simple view
          </a>
          {healthErr && <span className="badge badge-bad">API offline</span>}
          {health && (
            <span className={`badge ${health.llm_configured ? 'badge-ok' : 'badge-warn'}`}>
              {health.llm_configured ? `LLM: ${health.llm_model}` : 'LLM off — fallbacks active'}
            </span>
          )}
        </div>
      </header>
      <main>
        {page === 'plan' && <PlanPage />}
        {page === 'inbox' && <InboxPage />}
        {page === 'sku' && <SkuPage key={route[1] ?? ''} skuId={route[1]} />}
        {page === 'scenario' && <ScenarioPage />}
        {page === 'assistant' && <AssistantPage />}
        {page === 'activity' && <ActivityPage />}
        {!NAV.some(([k]) => k === page) && (
          <p>
            Unknown page. <a href="#/plan">Go to the plan</a>.
          </p>
        )}
      </main>
      <footer className="foot muted small">
        Money values are simulated INR over a 30-day horizon. Approving a plan only exports draft CSVs — nothing is
        sent to suppliers.
      </footer>
    </div>
  );
}
