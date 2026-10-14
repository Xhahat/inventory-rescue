import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Overview } from '../api';
import AskScreen from './AskScreen';
import MessageScreen from './MessageScreen';
import PlanScreen from './PlanScreen';
import './simple.css';

function Home({ offline }: { offline: boolean }) {
  const [ov, setOv] = useState<Overview | null>(null);
  useEffect(() => {
    api.overview().then(setOv).catch(() => setOv(null));
  }, []);
  const atRisk = ov ? new Set(ov.alerts.map((a) => a.entity_id)).size : 0;

  return (
    <div className="s-home">
      <h1>What would you like to do?</h1>
      {offline && <div className="s-problem">We can't reach the server right now. Please try again in a minute.</div>}
      {ov && atRisk > 0 && (
        <p className="s-lead">
          ⚠️ {atRisk} product{atRisk > 1 ? 's need' : ' needs'} attention. Making a plan will fix this.
        </p>
      )}
      <div className="s-tiles">
        <a className="s-tile s-tile-main" href="#/s/plan">
          <span className="s-tile-icon">🛒</span>
          <span className="s-tile-title">Make my plan</span>
          <span className="s-tile-text">Tell us your budget. We'll tell you what to buy and what to put on sale.</span>
        </a>
        <a className="s-tile" href="#/s/message">
          <span className="s-tile-icon">✉️</span>
          <span className="s-tile-title">
            Read a message
            {ov && ov.pending_signals > 0 && <span className="s-count">{ov.pending_signals}</span>}
          </span>
          <span className="s-tile-text">Paste a note from a supplier or your sales team. We'll find what matters.</span>
        </a>
        <a className="s-tile" href="#/s/ask">
          <span className="s-tile-icon">💬</span>
          <span className="s-tile-title">Ask a question</span>
          <span className="s-tile-text">Ask in your own words, like “What will run out first?”</span>
        </a>
      </div>
    </div>
  );
}

export default function SimpleApp({ screen }: { screen: string }) {
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    api.health().catch(() => setOffline(true));
  }, []);

  return (
    <div className="simple">
      <header className="s-top">
        <a className="s-brand" href="#/">
          <span className="logo">⟳</span> Inventory Rescue
        </a>
        <a className="s-advanced" href="#/plan" title="Charts, tables and tests for experienced planners">
          Advanced view
        </a>
      </header>
      <main className="s-main">
        {screen === 'plan' ? (
          <PlanScreen />
        ) : screen === 'message' ? (
          <MessageScreen />
        ) : screen === 'ask' ? (
          <AskScreen />
        ) : (
          <Home offline={offline} />
        )}
      </main>
      <footer className="s-foot">Nothing is ever sent to your suppliers without you.</footer>
    </div>
  );
}
