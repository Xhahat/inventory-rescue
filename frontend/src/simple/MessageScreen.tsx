import { useEffect, useRef, useState } from 'react';
import { api, waitForJob } from '../api';
import type { Signal } from '../api';
import { Hero, Problem, Working } from './parts';
import { Icon } from './icons';
import { signalSentence } from './plain';

function SignalAsk({
  s,
  nameOf,
  busy,
  onDecide,
}: {
  s: Signal;
  nameOf: (id: string) => string;
  busy: boolean;
  onDecide: (s: Signal, yes: boolean) => void;
}) {
  return (
    <div className={`s-card s-signal s-signal-${s.status}`}>
      <p className="s-sentence">{signalSentence(s, nameOf)}</p>
      {s.quoted_evidence && <p className="s-quote">The message said: “{s.quoted_evidence}”</p>}
      {s.status === 'pending' ? (
        <>
          <p className="s-question">Is this right?</p>
          <div className="s-row">
            <button className="s-btn s-yes" disabled={busy} onClick={() => onDecide(s, true)}>
              <Icon name="check" /> Yes, use this
            </button>
            <button className="s-btn s-no" disabled={busy} onClick={() => onDecide(s, false)}>
              <Icon name="x" /> No, ignore it
            </button>
          </div>
        </>
      ) : s.status === 'confirmed' ? (
        <p className="s-done">
          <Icon name="check" size={20} /> Got it. We will use this.
        </p>
      ) : (
        <p className="s-muted">Ignored.</p>
      )}
    </div>
  );
}

export default function MessageScreen() {
  const [names, setNames] = useState<Record<string, string>>({});
  const [demos, setDemos] = useState<{ name: string; text: string }[]>([]);
  const [demoIdx, setDemoIdx] = useState(0);
  const [text, setText] = useState('');
  const [found, setFound] = useState<Signal[] | null>(null);
  const [skipped, setSkipped] = useState(0);
  const [earlier, setEarlier] = useState<Signal[]>([]);
  const [busy, setBusy] = useState(false);
  const [updating, setUpdating] = useState(0);
  const [confirmedAny, setConfirmedAny] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const nameOf = (id: string) => names[id] ?? id;

  const loadEarlier = () =>
    api
      .signals()
      .then((all) => setEarlier(all.filter((s) => s.status === 'pending')))
      .catch(() => setEarlier([]));

  useEffect(() => {
    api
      .skus()
      .then((rows) => setNames(Object.fromEntries(rows.map((r) => [r.sku_id, r.name]))))
      .catch(() => undefined);
    api.demoMessages().then(setDemos).catch(() => setDemos([]));
    loadEarlier();
  }, []);

  const read = async (file?: File) => {
    setBusy(true);
    setError(null);
    try {
      const r = file ? await api.uploadMessage(file) : await api.postMessage(text);
      const usable = r.events.filter((e) => e.status !== 'invalid');
      setFound(usable);
      setSkipped(r.events.length - usable.length);
      const ids = new Set(usable.map((e) => e.id));
      setEarlier((cur) => cur.filter((s) => !ids.has(s.id)));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const decide = async (s: Signal, yes: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const r = yes ? await api.confirm(s.id) : await api.reject(s.id);
      const upd = (list: Signal[]) => list.map((x) => (x.id === r.signal.id ? r.signal : x));
      setFound((f) => (f ? upd(f) : f));
      setEarlier(upd);
      if (yes) setConfirmedAny(true);
      setBusy(false);
      if (r.job) {
        setUpdating((n) => n + 1);
        try {
          await waitForJob(r.job.id);
        } finally {
          setUpdating((n) => n - 1);
        }
      }
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const tryExample = () => {
    if (!demos.length) return;
    setText(demos[demoIdx % demos.length].text);
    setDemoIdx((i) => i + 1);
  };

  const reset = () => {
    setFound(null);
    setText('');
    setSkipped(0);
    loadEarlier();
  };

  const stillOpen = (found ?? []).some((s) => s.status === 'pending');
  const earlierOpen = earlier.filter((s) => s.status === 'pending').length;

  return (
    <div className="s-screen">
      <Hero icon="mail" title="Read a message" sub="Paste a note from a supplier or your sales team." />
      <div className="s-sheet">
      <Problem error={error} />

      {found === null ? (
        <>
          <p className="s-lead">Paste a message from a supplier, a customer or your sales team.</p>
          <textarea
            className="s-textarea"
            rows={7}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste the message here…"
            aria-label="Message"
          />
          {busy ? (
            <Working text="Reading the message… this takes a few seconds." />
          ) : (
            <div className="s-row">
              <button className="s-btn s-primary" disabled={!text.trim()} onClick={() => read()}>
                Read it
              </button>
              <button className="s-btn" onClick={() => fileRef.current?.click()}>
                Open a file
              </button>
              {demos.length > 0 && (
                <button className="s-link" onClick={tryExample}>
                  Show me an example
                </button>
              )}
            </div>
          )}
          <input
            ref={fileRef}
            type="file"
            accept=".txt,.eml,text/plain"
            hidden
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) read(f);
              e.target.value = '';
            }}
          />
        </>
      ) : (
        <>
          {found.length === 0 ? (
            <p className="s-lead">
              Nothing in this message changes your stock. There is nothing you need to do.
            </p>
          ) : (
            <p className="s-lead">
              We found {found.length === 1 ? '1 thing' : `${found.length} things`} in this message.
            </p>
          )}
          {skipped > 0 && (
            <p className="s-muted">
              We weren't sure about {skipped === 1 ? 'one part' : `${skipped} parts`} of the message, so we left{' '}
              {skipped === 1 ? 'it' : 'them'} out.
            </p>
          )}
          {found.map((s) => (
            <SignalAsk key={s.id} s={s} nameOf={nameOf} busy={busy} onDecide={decide} />
          ))}
          {!stillOpen && (
            <div className="s-row">
              <button className="s-btn" onClick={reset} disabled={busy}>
                Read another message
              </button>
            </div>
          )}
        </>
      )}

      {earlier.length > 0 && (
        <section className="s-section">
          <h2>
            {earlierOpen === 0
              ? 'From earlier messages'
              : `${earlierOpen === 1 ? '1 thing' : `${earlierOpen} things`} from earlier messages still need your answer`}
          </h2>
          {earlier.map((s) => (
            <SignalAsk key={s.id} s={s} nameOf={nameOf} busy={busy} onDecide={decide} />
          ))}
        </section>
      )}

      {updating > 0 && <Working text="Updating your numbers… please wait." />}
      {confirmedAny && updating === 0 && (
        <div className="s-next">
          <p>Your numbers are up to date. Make a new plan to use this news.</p>
          <a className="s-btn s-primary" href="#/s/plan">
            Make a new plan <Icon name="arrow" size={20} />
          </a>
        </div>
      )}
      </div>
    </div>
  );
}
