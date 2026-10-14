import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Overview } from '../api';
import AskScreen from './AskScreen';
import { Icon } from './icons';
import type { IconName } from './icons';
import MessageScreen from './MessageScreen';
import PlanScreen from './PlanScreen';
import { day } from './plain';
import './simple.css';

function Tile({ href, icon, title, text, badge, main }: {
  href: string; icon: IconName; title: string; text: string; badge?: number; main?: boolean;
}) {
  return (
    <a className={`s-tile${main ? ' s-tile-main' : ''}`} href={href}>
      <span className="s-tile-icon">
        <Icon name={icon} size={26} />
      </span>
      <span className="s-tile-body">
        <span className="s-tile-title">
          {title}
          {!!badge && <span className="s-count">{badge} new</span>}
        </span>
        <span className="s-tile-text">{text}</span>
      </span>
      <span className="s-tile-go">
        <Icon name="arrow" size={22} />
      </span>
    </a>
  );
}

function Home({ offline }: { offline: boolean }) {
  const [ov, setOv] = useState<Overview | null>(null);
  useEffect(() => {
    api.overview().then(setOv).catch(() => setOv(null));
  }, []);
  const atRisk = ov ? new Set(ov.alerts.map((a) => a.entity_id)).size : 0;

  return (
    <div className="s-home">
      {ov && <p className="s-eyebrow">Stock as of {day(ov.as_of)}</p>}
      <h1>What would you like to do?</h1>
      {offline && <div className="s-problem">We can't reach the server right now. Please try again in a minute.</div>}
      {ov && (
        <p className={`s-status ${atRisk ? 's-status-warn' : 's-status-ok'}`}>
          <span className="s-status-dot" />
          {atRisk
            ? `${atRisk} product${atRisk > 1 ? 's need' : ' needs'} attention. A plan will fix this.`
            : 'All products look fine.'}
        </p>
      )}
      <nav className="s-tiles">
        <Tile main href="#/s/plan" icon="cart" title="Make my plan"
              text="Tell us your budget. We'll say what to buy and what to put on sale." />
        <Tile href="#/s/message" icon="mail" title="Read a message" badge={ov?.pending_signals}
              text="Paste a note from a supplier or your sales team." />
        <Tile href="#/s/ask" icon="chat" title="Ask a question" text="Ask in your own words, like “What will run out first?”" />
      </nav>
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
          <span className="s-mark" aria-hidden>IR</span> Inventory Rescue
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
