import { useCallback, useEffect, useRef, useState } from 'react';
import { api, inr, pct, waitForJob } from '../api';
import type { IngestResult, Job, Signal } from '../api';
import { evalApi } from '../api_eval';
import type { EvalJob, ExtractionEval, ExtractionMetrics, UnitRow } from '../api_eval';
import { Badge, Card, ErrorBox, Kpi, MethodBadge, Spinner } from '../ui';

const STATUS_TONE: Record<string, string> = { pending: 'warn', confirmed: 'ok', rejected: 'neutral', invalid: 'bad' };
const EVENT_LABEL: Record<string, string> = {
  supplier_delay: 'Supplier delay',
  demand_spike: 'Demand spike',
  demand_drop: 'Demand drop',
  expiry_risk: 'Expiry risk',
  other: 'Other',
};

function magnitudeText(s: Signal): string {
  if (s.event_type === 'supplier_delay') return `+${s.magnitude} days`;
  if (s.event_type.startsWith('demand')) return `×${s.magnitude} demand`;
  if (s.event_type === 'expiry_risk') return s.magnitude ? `${s.magnitude} units at risk` : '';
  return s.magnitude ? String(s.magnitude) : '';
}

function JobLine({ job }: { job: Job }) {
  const res = job.result as { computed?: Record<string, string>; reused?: string[] } | null;
  return (
    <div className="job">
      Job #{job.id} {job.kind} ({String(job.payload.trigger ?? '')}):{' '}
      {job.status === 'done' ? (
        <Badge kind="ok">done</Badge>
      ) : job.status === 'failed' ? (
        <Badge kind="bad">failed</Badge>
      ) : (
        <Spinner label={job.status} />
      )}
      {res?.computed && (
        <span className="muted small">
          {' '}
          re-forecast:{' '}
          {Object.entries(res.computed)
            .map(([k, v]) => `${k} (${v})`)
            .join(', ') || 'none'}
          {res.reused?.length ? ` · reused ${res.reused.length}` : ''}
        </span>
      )}
      {job.error && <span className="error-inline"> {job.error}</span>}
      {job.status === 'done' && (
        <span>
          {' '}
          → <a href="#/plan">Re-plan with the new forecast</a>
        </span>
      )}
    </div>
  );
}

function SignalCard({
  s,
  onDecide,
  busy,
}: {
  s: Signal;
  onDecide: (s: Signal, d: 'confirm' | 'reject') => void;
  busy: boolean;
}) {
  return (
    <div className={`signal signal-${s.status}`}>
      <div className="signal-head">
        <strong>{s.ref}</strong> <Badge kind={STATUS_TONE[s.status] ?? 'neutral'}>{s.status}</Badge>{' '}
        <strong>{EVENT_LABEL[s.event_type] ?? s.event_type}</strong> <span>{magnitudeText(s)}</span>
        <span className="muted small">
          {' '}
          {s.start_date ?? '?'} → {s.end_date ?? '?'}
        </span>
      </div>
      <div className="chips">
        {s.sku_ids.map((k) => (
          <a key={k} className="chip" href={`#/sku/${k}`}>
            {k}
          </a>
        ))}
        {s.sku_ids.length === 0 && <span className="muted small">no SKUs</span>}
      </div>
      {s.quoted_evidence && <blockquote>“{s.quoted_evidence}”</blockquote>}
      {s.rejection_reason && (
        <div className={s.status === 'invalid' ? 'error-inline' : 'muted small'}>
          {s.status === 'invalid' ? 'Failed validation: ' : 'Reason: '}
          {s.rejection_reason}
        </div>
      )}
      {(s.status === 'pending' || s.status === 'confirmed' || s.status === 'rejected') && (
        <div className="signal-actions">
          {s.status !== 'confirmed' && (
            <button className="primary" disabled={busy} onClick={() => onDecide(s, 'confirm')}>
              Confirm
            </button>
          )}
          {s.status !== 'rejected' && (
            <button disabled={busy} onClick={() => onDecide(s, 'reject')}>
              Reject
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function unitText(u: UnitRow, predicted: boolean): string {
  const m = predicted ? u.predicted_magnitude : u.expected_magnitude;
  const range = !predicted && u.magnitude_range ? ` [${u.magnitude_range.join('–')}]` : '';
  return `${EVENT_LABEL[u.event_type] ?? u.event_type} ${u.sku_id}${m !== undefined && m !== null ? ` ${m}` : ''}${range}`;
}

const METRIC_ROWS: [keyof ExtractionMetrics, string, string][] = [
  ['precision', 'Precision', 'valid extracted (type, SKU) units that are in the gold set'],
  ['recall', 'Recall', 'gold (type, SKU) units that were extracted'],
  ['f1', 'F1', 'harmonic mean of precision and recall'],
  ['magnitude_accuracy', 'Magnitude within tolerance', 'matched units whose days / multiplier / units are in range'],
  ['start_date_accuracy', 'Start date (±1 day)', 'demand events with a gold start date'],
  ['evidence_valid_rate', 'Evidence quote verbatim', 'raw model events whose quote appears in the message'],
  ['invalid_rate', 'Rejected by validators', 'raw events that never reach the confirm queue'],
  ['false_positive_rate_no_event', 'False positives on no-event messages', 'lower is better'],
  ['message_accuracy', 'Messages fully correct', 'no miss, no false positive, magnitudes in range'],
];

function ExtractionQualityCard() {
  const [data, setData] = useState<ExtractionEval | null>(null);
  const [job, setJob] = useState<EvalJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onlyMisses, setOnlyMisses] = useState(true);

  const load = useCallback(
    () =>
      evalApi
        .extraction()
        .then((r) => {
          setData(r.result);
          setJob(r.job);
        })
        .catch((e) => setError(e.message)),
    [],
  );

  useEffect(() => {
    load();
  }, [load]);

  const run = async () => {
    setError(null);
    try {
      const j = await evalApi.runExtraction();
      setJob(j);
      const done = await waitForJob(j.id, (x) => setJob(x), 600_000);
      if (done.status === 'failed') setError(done.error ?? 'evaluation failed');
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const running = job?.status === 'queued' || job?.status === 'running';
  const v = data?.value_of_signals;
  const rows = (data?.per_message ?? []).filter((m) => !onlyMisses || !m.correct);

  return (
    <Card
      title="Extraction quality"
      actions={
        <button className="primary" disabled={running} onClick={run}>
          {running ? <Spinner label={job?.status} /> : data ? 'Re-run evaluation' : 'Run evaluation'}
        </button>
      }
    >
      <p className="muted small">
        The real extraction path (same prompt and validators) on a labelled gold set of inbox messages: supplier
        emails, WhatsApp notes, Hindi/Hinglish, forwarded threads, retractions, vague maybes, unknown products and
        no-event messages. The rule-based fallback parser is scored on the same messages for comparison.
      </p>
      <ErrorBox error={error} />
      {job?.status === 'failed' && <p className="error-inline">Last run failed: {job.error}</p>}
      {!data && !running && <p className="muted">Not evaluated yet.</p>}
      {data && (
        <>
          <p className="muted small">
            {data.gold_messages} messages · model {data.model ?? 'none'} · as of {data.as_of} · computed{' '}
            {new Date(data.computed_at).toLocaleString()} · LLM {data.llm_seconds}s (cache {data.cache.hits ?? 0} hits
            / {data.cache.misses ?? 0} calls){data.llm && data.llm.fallbacks ? ` · ${data.llm.fallbacks} fallbacks` : ''}
          </p>
          <table className="table compact">
            <thead>
              <tr>
                <th>Metric</th>
                <th className="num">LLM</th>
                <th className="num">Rule-based</th>
                <th>Meaning</th>
              </tr>
            </thead>
            <tbody>
              {METRIC_ROWS.map(([k, label, hint]) => (
                <tr key={k}>
                  <td>{label}</td>
                  <td className="num">{data.llm ? pct(data.llm[k] as number | null, 1) : '—'}</td>
                  <td className="num">{pct(data.heuristic[k] as number | null, 1)}</td>
                  <td className="muted small">{hint}</td>
                </tr>
              ))}
              <tr>
                <td>Gold / predicted units</td>
                <td className="num">
                  {data.llm ? `${data.llm.n_gold_units} / ${data.llm.n_predicted_units}` : '—'}
                </td>
                <td className="num">
                  {data.heuristic.n_gold_units} / {data.heuristic.n_predicted_units}
                </td>
                <td className="muted small">{data.heuristic.n_no_event_messages} messages have no actionable event</td>
              </tr>
            </tbody>
          </table>

          {v && (
            <>
              <h3>Value of extracted signals</h3>
              <div className="kpis">
                <Kpi
                  label="Perfect-information value captured"
                  value={pct(v.captured_actual, 0)}
                  sub={`Monte Carlo mean (${v.n_mc_worlds} worlds): ${pct(v.captured_mc, 0)}`}
                  tone={v.captured_actual !== null && v.captured_actual >= 0.8 ? 'good' : undefined}
                />
                <Kpi label="Plan loss, no signals" value={inr(v.actual_world_loss.none)} sub={`MC mean ${inr(v.mc_mean_loss.none)}`} />
                <Kpi
                  label={`Plan loss, ${v.extractor === 'llm' ? 'LLM' : 'rule-based'} signals`}
                  value={inr(v.actual_world_loss.llm)}
                  sub={`${v.n_signals.llm} signals · MC mean ${inr(v.mc_mean_loss.llm)}`}
                />
                <Kpi
                  label="Plan loss, gold signals"
                  value={inr(v.actual_world_loss.gold)}
                  sub={`${v.n_signals.gold} signals · MC mean ${inr(v.mc_mean_loss.gold)}`}
                />
              </div>
              <p className="muted small">
                Messages {v.messages.join(', ')} (the inbox messages behind evaluation-truth events). {v.note}
              </p>
            </>
          )}

          <div className="toolbar">
            <h3>Per message</h3>
            <label className="small">
              <input type="checkbox" checked={onlyMisses} onChange={(e) => setOnlyMisses(e.target.checked)} /> only
              messages with errors
            </label>
          </div>
          {rows.length === 0 && <p className="muted small">Every message is fully correct.</p>}
          {rows.map((m) => (
            <details key={m.id} className="eval-msg">
              <summary>
                <strong>{m.id}</strong> {m.correct ? <Badge kind="ok">correct</Badge> : <Badge kind="bad">errors</Badge>}
                {m.method !== 'llm' && <Badge kind="warn">{m.method}</Badge>}
                <span className="muted small">
                  {' '}
                  {m.tags.join(', ')} · TP {m.tp} · FP {m.fp} · FN {m.fn}
                </span>
              </summary>
              <blockquote>{m.text}</blockquote>
              {m.misses.map((u, i) => (
                <div key={`m${i}`} className="error-inline">
                  Missed: {unitText(u, false)}
                </div>
              ))}
              {m.false_positives.map((u, i) => (
                <div key={`f${i}`} className="error-inline">
                  False positive: {unitText(u, true)}
                </div>
              ))}
              {m.matched
                .filter((u) => u.magnitude_ok === false || u.start_ok === false)
                .map((u, i) => (
                  <div key={`w${i}`} className="error-inline">
                    Wrong {u.magnitude_ok === false ? `magnitude ${u.predicted_magnitude}` : ''}
                    {u.start_ok === false ? ` start ${u.predicted_start ?? '?'} (expected ${u.expected_start})` : ''}:{' '}
                    {unitText(u, false)}
                  </div>
                ))}
              {m.matched
                .filter((u) => u.magnitude_ok !== false && u.start_ok !== false)
                .map((u, i) => (
                  <div key={`ok${i}`} className="muted small">
                    Matched: {unitText(u, false)} → {u.predicted_magnitude}
                  </div>
                ))}
              {m.invalid.map((x, i) => (
                <div key={`i${i}`} className="muted small">
                  Rejected by validator: {EVENT_LABEL[x.event_type] ?? x.event_type} {(x.sku_ids ?? []).join(', ')}:{' '}
                  {x.reason}
                </div>
              ))}
            </details>
          ))}
          <p className="muted small">{data.note}</p>
        </>
      )}
    </Card>
  );
}

export default function InboxPage() {
  const [demos, setDemos] = useState<{ name: string; text: string }[]>([]);
  const [text, setText] = useState('');
  const [signals, setSignals] = useState<Signal[]>([]);
  const [last, setLast] = useState<IngestResult | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importMsg, setImportMsg] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>('all');
  const fileRef = useRef<HTMLInputElement>(null);
  const csvRef = useRef<HTMLInputElement>(null);

  const loadSignals = useCallback(() => api.signals().then(setSignals).catch((e) => setError(e.message)), []);

  useEffect(() => {
    api.demoMessages().then(setDemos).catch(() => setDemos([]));
    loadSignals();
  }, [loadSignals]);

  const track = async (job: Job) => {
    setJobs((js) => [job, ...js.filter((j) => j.id !== job.id)]);
    await waitForJob(job.id, (j) => setJobs((js) => js.map((x) => (x.id === j.id ? j : x))));
  };

  const ingest = async (fn: () => Promise<IngestResult>) => {
    setBusy(true);
    setError(null);
    try {
      setLast(await fn());
      await loadSignals();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const decide = async (s: Signal, d: 'confirm' | 'reject') => {
    let reason: string | undefined;
    if (d === 'reject') {
      reason = window.prompt('Reason for rejecting (optional)') ?? undefined;
    }
    setBusy(true);
    setError(null);
    try {
      const r = d === 'confirm' ? await api.confirm(s.id) : await api.reject(s.id, reason);
      await loadSignals();
      setLast((l) => (l ? { ...l, events: l.events.map((e) => (e.id === r.signal.id ? r.signal : e)) } : l));
      setBusy(false);
      if (r.job) await track(r.job);
    } catch (e) {
      setError((e as Error).message);
      setBusy(false);
    }
  };

  const importCsv = async (f: File) => {
    setError(null);
    setImportMsg(null);
    try {
      const r = await api.importSales(f);
      setImportMsg(`Imported ${r.imported} sales rows for ${r.skus.length} SKU(s). Re-forecasting…`);
      await track(r.job);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const shown = filter === 'all' ? signals : signals.filter((s) => s.status === filter);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Signal Inbox</h1>
          <p className="muted">
            Paste supplier or campaign messages. Extracted events need your confirmation before they change any
            forecast.
          </p>
        </div>
      </div>
      <ErrorBox error={error} />

      <div className="grid2">
        <Card title="New message">
          <div className="toolbar wrap">
            <span className="muted small">Demo messages:</span>
            {demos.map((d) => (
              <button key={d.name} onClick={() => setText(d.text)} disabled={busy}>
                {d.name.replace(/^\d+_/, '').replace(/_/g, ' ')}
              </button>
            ))}
          </div>
          <textarea
            rows={9}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="e.g. Hi team, QuickPack here — the shipment on PO-2041 will slip by about 9 days…"
          />
          <div className="toolbar">
            <button
              className="primary"
              disabled={busy || !text.trim()}
              onClick={() => ingest(() => api.postMessage(text))}
            >
              {busy ? <Spinner label="Extracting…" /> : 'Extract signals'}
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".txt,.eml,text/plain"
              hidden
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) ingest(() => api.uploadMessage(f));
                e.target.value = '';
              }}
            />
            <button disabled={busy} onClick={() => fileRef.current?.click()}>
              Upload .txt
            </button>
          </div>
        </Card>

        <Card title="Last extraction">
          {!last && <p className="muted">Nothing extracted yet in this session.</p>}
          {last && (
            <>
              <p>
                Message #{last.message_id} · <MethodBadge method={last.extraction_method} reason={last.llm_error} /> ·{' '}
                {last.events.length} event(s)
              </p>
              {last.llm_error && <p className="muted small">LLM output rejected: {last.llm_error}</p>}
              {last.events.map((s) => (
                <SignalCard key={s.id} s={s} onDecide={decide} busy={busy} />
              ))}
            </>
          )}
          {jobs.map((j) => (
            <JobLine key={j.id} job={j} />
          ))}
        </Card>
      </div>

      <Card
        title={`All signals (${signals.length})`}
        actions={
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            {['all', 'pending', 'confirmed', 'rejected', 'invalid'].map((f) => (
              <option key={f} value={f}>
                {f}
              </option>
            ))}
          </select>
        }
      >
        {shown.length === 0 ? (
          <p className="muted">No signals.</p>
        ) : (
          <div className="signal-grid">
            {shown.map((s) => (
              <SignalCard key={s.id} s={s} onDecide={decide} busy={busy} />
            ))}
          </div>
        )}
      </Card>

      <Card title="Import sales CSV">
        <p className="muted small">
          Columns: <code>sale_date, sku_id, channel, segment, units</code>. The whole file is rejected if any row is
          invalid. A successful import triggers a re-forecast job.
        </p>
        <input
          ref={csvRef}
          type="file"
          accept=".csv,text/csv"
          hidden
          onChange={(e) => {
            const f = e.target.files?.[0];
            if (f) importCsv(f);
            e.target.value = '';
          }}
        />
        <button onClick={() => csvRef.current?.click()}>Choose CSV…</button>
        {importMsg && <p>{importMsg}</p>}
        <p className="muted small">Signal magnitudes for demand are multipliers (e.g. 1.6 = {pct(0.6)} uplift).</p>
      </Card>

      <ExtractionQualityCard />
    </div>
  );
}
