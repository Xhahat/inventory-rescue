import { useEffect, useState } from 'react';
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api, inr, num, pct, waitForJob } from '../api';
import type { Backtest, Job, Plan, ScenarioResult, Totals } from '../api';
import { Badge, Card, ErrorBox, MethodBadge, Spinner } from '../ui';

const EXAMPLES = [
  'What if demand rises 20% and QuickPack is delayed 7 days?',
  'What if demand for SKU-019 doubles for two weeks?',
  'What if supplier SUP-02 has an outage?',
  'What if our budget is cut to 30000?',
];

const LOSS_PARTS: [keyof Totals, string, string][] = [
  ['lost_margin', 'Lost margin (stockouts)', '#ef4444'],
  ['expiry_writeoff', 'Expiry write-off', '#f59e0b'],
  ['markdown_cost', 'Markdown cost', '#a855f7'],
  ['expedite_premium', 'Expedite premium', '#3b82f6'],
];

const ROWS: [keyof Totals, string, 'inr' | 'num'][] = [
  ['total_loss', 'Total loss', 'inr'],
  ['lost_margin', 'Lost margin', 'inr'],
  ['expiry_writeoff', 'Expiry write-off', 'inr'],
  ['markdown_cost', 'Markdown cost', 'inr'],
  ['expedite_premium', 'Expedite premium', 'inr'],
  ['cash_outlay', 'Cash outlay', 'inr'],
  ['units_short', 'Units short', 'num'],
  ['stockout_sku_days', 'Stockout SKU-days', 'num'],
  ['expired_units', 'Expired units', 'num'],
  ['markdown_units', 'Markdown units', 'num'],
  ['ending_units', 'Ending units', 'num'],
  ['ending_value', 'Ending inventory value', 'inr'],
];

function TotalsTable({ cols }: { cols: [string, Totals][] }) {
  return (
    <table className="table compact">
      <thead>
        <tr>
          <th />
          {cols.map(([h]) => (
            <th key={h} className="num">
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {ROWS.map(([k, label, f]) => (
          <tr key={k} className={k === 'total_loss' ? 'strong' : ''}>
            <td>{label}</td>
            {cols.map(([h, t]) => (
              <td key={h} className="num">
                {f === 'inr' ? inr(t[k]) : num(t[k])}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function LossChart({ cols }: { cols: [string, Totals][] }) {
  const data = cols.map(([name, t]) => ({ name, ...Object.fromEntries(LOSS_PARTS.map(([k]) => [k, t[k]])) }));
  return (
    <ResponsiveContainer width="100%" height={260}>
      <BarChart data={data} margin={{ top: 10, right: 20, left: 10, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="name" fontSize={12} />
        <YAxis fontSize={12} tickFormatter={(v: number) => `₹${Math.round(v / 1000)}k`} />
        <Tooltip formatter={(v) => inr(Number(v))} />
        <Legend />
        {LOSS_PARTS.map(([k, label, color]) => (
          <Bar key={k} dataKey={k} name={label} stackId="loss" fill={color} isAnimationActive={false} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

function specText(spec: Record<string, unknown>): string {
  const parts: string[] = [];
  const m = Number(spec.demand_multiplier ?? 1);
  const skus = (spec.affected_skus as string[] | undefined) ?? [];
  if (m !== 1) parts.push(`demand ×${m} on ${skus.length ? skus.join(', ') : 'all SKUs'}`);
  const lt = Number(spec.lead_time_delta_days ?? 0);
  if (lt) parts.push(`lead time ${lt > 0 ? '+' : ''}${lt} days for ${spec.delay_supplier ?? (skus.length ? skus.join(', ') : 'all SKUs')}`);
  if (spec.supplier_outage) parts.push(`outage at ${spec.supplier_outage}`);
  if (spec.budget !== null && spec.budget !== undefined) parts.push(`budget ${inr(Number(spec.budget))}`);
  parts.push(`days ${spec.start_day ?? 0}–${spec.end_day ?? 30}`);
  return parts.join(' · ');
}

export default function ScenarioPage() {
  const [plan, setPlan] = useState<Plan | null>(null);
  const [q, setQ] = useState(EXAMPLES[0]);
  const [res, setRes] = useState<ScenarioResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [bt, setBt] = useState<Backtest | null>(null);
  const [btJob, setBtJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.latestPlan().then(setPlan).catch((e) => setError(e.message));
    api.backtest().then(setBt).catch(() => setBt(null));
  }, []);

  const ask = async () => {
    setBusy(true);
    setError(null);
    try {
      setRes(await api.scenario(q, plan?.budget ?? null));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const runBt = async () => {
    setError(null);
    try {
      const j = await api.runBacktest();
      setBtJob(j);
      const done = await waitForJob(j.id, setBtJob, 600_000);
      if (done.status === 'done') setBt(await api.backtest());
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const s = plan?.summary;
  const btRunning = btJob && (btJob.status === 'queued' || btJob.status === 'running');

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Scenario &amp; Value</h1>
          <p className="muted">Ask what-if questions and see how the plan compares with a rule-based baseline.</p>
        </div>
      </div>
      <ErrorBox error={error} />

      <Card title="What-if scenario">
        <div className="toolbar wrap">
          {EXAMPLES.map((e) => (
            <button key={e} className="ghost small" onClick={() => setQ(e)}>
              {e}
            </button>
          ))}
        </div>
        <div className="toolbar">
          <input className="grow" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && ask()} />
          <button className="primary" onClick={ask} disabled={busy || !q.trim()}>
            {busy ? <Spinner label="Simulating…" /> : 'Run scenario'}
          </button>
        </div>
        {res && (
          <>
            <p>
              <MethodBadge method={res.parse_method} reason={res.parse_error} /> Understood as:{' '}
              <strong>{specText(res.spec)}</strong>
              {res.spec.summary ? <span className="muted"> — “{String(res.spec.summary)}”</span> : null}
            </p>
            {res.parse_error && <p className="muted small">LLM parse rejected: {res.parse_error}</p>}
            <div className="grid2">
              <div>
                <LossChart
                  cols={[
                    ['Baseline', res.outcomes_in_scenario.baseline],
                    ['Current plan', res.outcomes_in_scenario.current_plan],
                    ['Adapted plan', res.outcomes_in_scenario.adapted_plan],
                  ]}
                />
              </div>
              <div>
                <TotalsTable
                  cols={[
                    ['Baseline', res.outcomes_in_scenario.baseline],
                    ['Current plan', res.outcomes_in_scenario.current_plan],
                    ['Adapted plan', res.outcomes_in_scenario.adapted_plan],
                  ]}
                />
                <p className="small">
                  Current plan: budget {inr(res.current_budget)}, uses {inr(res.current_cash_used)} · adapted plan:
                  budget {inr(res.budget)}, uses {inr(res.adapted_cash_used)}. All three are scored in the scenario
                  world.
                </p>
                {res.outcomes_in_scenario.adapted_plan.total_loss > res.outcomes_in_scenario.current_plan.total_loss &&
                  res.budget >= res.current_budget && (
                    <p className="notice warn small">
                      The adapted plan is optimised on the forecast, but the scenario is scored against simulated
                      actual demand, so here the current plan does better. Forecast error matters more than the
                      scenario change in this case.
                    </p>
                  )}
              </div>
            </div>
            <h3>What changes in the adapted plan ({res.changed_actions.length})</h3>
            {res.changed_actions.length === 0 ? (
              <p className="muted">The current plan already holds up — no action changes.</p>
            ) : (
              <table className="table compact">
                <thead>
                  <tr>
                    <th>SKU</th>
                    <th>Before</th>
                    <th>After</th>
                  </tr>
                </thead>
                <tbody>
                  {res.changed_actions.map((c) => (
                    <tr key={c.sku_id}>
                      <td>
                        <a href={`#/sku/${c.sku_id}`}>{c.sku_id}</a> <span className="muted small">{c.sku_name}</span>
                      </td>
                      <td>{c.before}</td>
                      <td className="strong">{c.after}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </>
        )}
      </Card>

      {!plan && (
        <Card>
          <p>
            No plan yet — <a href="#/plan">generate one</a> to see the value scorecard.
          </p>
        </Card>
      )}

      {plan && s && (
        <>
          <Card
            title={
              <>
                Value scorecard (plan #{plan.id}, budget {inr(plan.budget)}){' '}
                <Badge kind={s.scorecard.savings >= 0 ? 'ok' : 'bad'}>savings {inr(s.scorecard.savings)}</Badge>
              </>
            }
          >
            <div className="grid2">
              <LossChart
                cols={[
                  ['Rule baseline', s.scorecard.baseline],
                  ['Recommended plan', s.scorecard.plan],
                ]}
              />
              <TotalsTable
                cols={[
                  ['Rule baseline', s.scorecard.baseline],
                  ['Recommended plan', s.scorecard.plan],
                ]}
              />
            </div>
            <div className="grid2 small">
              <div>
                <h3>Evaluation world (scores both)</h3>
                <ul>
                  {Object.entries(s.evaluation_world).map(([k, v]) => (
                    <li key={k}>
                      <strong>{k}:</strong> {String(v)}
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <h3>Planning world (what the planner optimises)</h3>
                <ul>
                  {Object.entries(s.planning_world).map(([k, v]) => (
                    <li key={k}>
                      <strong>{k}:</strong> {String(v)}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
            <p className="muted small">
              All figures are simulated over 30 days. They are evidence for the decision logic, not a promise of
              realised savings.
            </p>
          </Card>

          <Card title="Sensitivity: does the plan still beat the baseline?">
            <table className="table compact">
              <thead>
                <tr>
                  <th>Case</th>
                  <th className="num">Baseline loss</th>
                  <th className="num">Plan loss</th>
                  <th className="num">Savings</th>
                </tr>
              </thead>
              <tbody>
                {s.sensitivity.map((r) => (
                  <tr key={r.case}>
                    <td>{r.case}</td>
                    <td className="num">{inr(r.baseline_total_loss)}</td>
                    <td className="num">{inr(r.plan_total_loss)}</td>
                    <td className={`num strong ${r.savings < 0 ? 'bad-text' : 'good-text'}`}>{inr(r.savings)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </>
      )}

      <Card
        title="Forecast backtest (14-day hold-out)"
        actions={
          <button onClick={runBt} disabled={!!btRunning}>
            {btRunning ? <Spinner label={btJob?.status} /> : bt ? 'Re-run backtest' : 'Run backtest'}
          </button>
        }
      >
        {btJob?.status === 'failed' && <ErrorBox error={btJob.error} />}
        {!bt && <p className="muted">Not run yet. It forecasts the last 14 days from earlier data for the top 10 SKUs.</p>}
        {bt && (
          <>
            <p>
              Hold-out from <strong>{bt.holdout_start}</strong> ({bt.holdout_days} days, {bt.skus} SKUs) · valid LLM
              forecasts: <strong>{bt.llm_valid_forecasts}</strong>/{bt.skus}
            </p>
            <div className="kpis">
              <div className="kpi">
                <div className="kpi-label">WAPE seasonal-naive (all)</div>
                <div className="kpi-value">{pct(bt.wape_naive_all, 1)}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">WAPE LLM (valid only)</div>
                <div className="kpi-value">{pct(bt.wape_llm_on_valid, 1)}</div>
              </div>
              <div className="kpi">
                <div className="kpi-label">WAPE naive (same SKUs)</div>
                <div className="kpi-value">{pct(bt.wape_naive_on_valid, 1)}</div>
              </div>
            </div>
            <table className="table compact">
              <thead>
                <tr>
                  <th>SKU</th>
                  <th>Method</th>
                  <th className="num">WAPE LLM</th>
                  <th className="num">WAPE naive</th>
                </tr>
              </thead>
              <tbody>
                {bt.per_sku.map((r) => (
                  <tr key={r.sku_id}>
                    <td>
                      <a href={`#/sku/${r.sku_id}`}>{r.sku_id}</a>
                    </td>
                    <td>
                      <MethodBadge method={r.llm_method} />
                    </td>
                    <td className="num">{pct(r.wape_llm, 1)}</td>
                    <td className="num">{pct(r.wape_naive, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <p className="muted small">
              {bt.note} Computed {new Date(bt.computed_at).toLocaleString()}.
            </p>
          </>
        )}
      </Card>
    </div>
  );
}
