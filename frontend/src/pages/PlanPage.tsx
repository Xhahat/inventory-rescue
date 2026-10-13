import { useCallback, useEffect, useState } from 'react';
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { api, inr, num, pct } from '../api';
import type { Dist, OverrideItem, Overview, Plan, PlanAction, PlanDiff, Robustness, Strategy } from '../api';
import { AlertList, Badge, Card, ErrorBox, Kpi, MethodBadge, PriorityBadge, RecordChip, Spinner } from '../ui';

const KIND: Record<string, [string, string]> = {
  standard_po: ['ok', 'buy'],
  expedited_po: ['bad', 'expedite'],
  staggered_po: ['ok', 'staggered buy'],
  markdown: ['warn', 'markdown'],
  defer: ['neutral', 'defer'],
};

const STRATEGY_LABEL: Record<Strategy, string> = {
  commit_now: 'Commit now',
  staggered: 'Staggered buys',
  reserve: 'Keep a reserve',
  lean: 'Lean budget',
};

const POLICY_LABEL: Record<string, string> = {
  plan: 'Inventory Rescue plan',
  static_baseline: 'Static reorder rule',
  tuned_baseline: 'Tuned reorder rule',
  plan_without_signals: 'Plan without confirmed signals',
  plan_once: 'Plan once (day 0 only)',
  replan_weekly: 'Re-plan weekly',
  baseline_weekly: 'Reorder rule, weekly',
};

function KindBadges({ kind }: { kind: string }) {
  return (
    <>
      {kind.split('+').map((k) => {
        const [tone, label] = KIND[k] ?? ['neutral', k];
        return (
          <Badge key={k} kind={tone}>
            {label}
          </Badge>
        );
      })}
    </>
  );
}

type Edit = { qty?: string; pct?: string; skip?: boolean };

function ActionRow({
  a,
  hero,
  editing,
  edit,
  onEdit,
}: {
  a: PlanAction;
  hero: boolean;
  editing: boolean;
  edit: Edit | undefined;
  onEdit: (e: Edit) => void;
}) {
  const [open, setOpen] = useState(false);
  const cls = [editing ? '' : 'clickable', a.overridden ? 'overridden' : '', edit?.skip ? 'skipped' : '']
    .filter(Boolean)
    .join(' ');
  return (
    <>
      <tr className={cls} onClick={() => !editing && setOpen(!open)}>
        <td>{a.rank}</td>
        <td>
          <a href={`#/sku/${a.sku_id}`} onClick={(e) => e.stopPropagation()}>
            {a.sku_id}
          </a>{' '}
          {hero && <PriorityBadge priority="hero" />}
          <div className="muted small">{a.sku_name}</div>
        </td>
        <td>
          <KindBadges kind={a.kind} />
          {a.overridden && (
            <Badge kind="warn" title={`Optimiser suggested: ${a.original}`}>
              edited
            </Badge>
          )}
        </td>
        <td>
          {editing ? (
            <span className="row-gap">
              <label className="small">
                qty{' '}
                <input
                  className="w-qty"
                  type="number"
                  min={0}
                  value={edit?.qty ?? String(a.order_qty)}
                  onChange={(e) => onEdit({ ...edit, qty: e.target.value })}
                />
              </label>{' '}
              <label className="small">
                % off{' '}
                <input
                  className="w-pct"
                  type="number"
                  min={0}
                  max={60}
                  step={5}
                  value={edit?.pct ?? String(Math.round((a.discount_pct ?? 0) * 100))}
                  onChange={(e) => onEdit({ ...edit, pct: e.target.value })}
                />
              </label>{' '}
              <label className="small">
                <input type="checkbox" checked={!!edit?.skip} onChange={(e) => onEdit({ ...edit, skip: e.target.checked })} />{' '}
                skip
              </label>
            </span>
          ) : (
            <>
              {a.label}
              {!!a.later_qty && (
                <div className="small">
                  {a.order_qty} now (arrives ~{a.arrival_date}) · <strong>+{a.later_qty}</strong> ordered{' '}
                  {a.later_order_date} (arrives ~{a.later_arrival_date}), re-checked at that review
                </div>
              )}
              {a.overridden && <div className="muted small">was: {a.original}</div>}
            </>
          )}
        </td>
        <td className="num">
          {inr(a.cash_outlay)}
          {!!a.later_qty && <div className="range">{inr(a.cash_today)} today</div>}
        </td>
        <td className="num">{inr(a.expected_loss_if_deferred)}</td>
        <td className="num">{inr(a.expected_loss_with_action)}</td>
        <td className="num strong">{inr(a.loss_avoided)}</td>
        <td>
          <MethodBadge method={a.forecast_method} />
        </td>
        <td className="muted">{editing ? '' : open ? '▾' : '▸'}</td>
      </tr>
      {open && !editing && (
        <tr className="detail-row">
          <td />
          <td colSpan={9}>
            <ul className="reasons">
              {a.reasons.map((r, i) => (
                <li key={i}>{r}</li>
              ))}
            </ul>
            <div className="chips">
              <span className="muted small">Records used: </span>
              {a.record_ids.map((id) => (
                <RecordChip key={id} id={id} />
              ))}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function DiffCard({ d }: { d: PlanDiff }) {
  return (
    <Card title={`What changed since plan #${d.previous_plan_id}`}>
      <p className="small">
        Previous plan: as of {d.previous_as_of}, budget {inr(d.previous_budget)} ·{' '}
        <span className="change-added">{d.n_added} added</span> ·{' '}
        <span className="change-removed">{d.n_removed} removed</span> ·{' '}
        <span className="change-changed">{d.n_changed} changed</span> · {d.unchanged} unchanged · expected loss{' '}
        {d.delta_expected_loss <= 0 ? '−' : '+'}
        {inr(Math.abs(d.delta_expected_loss))} · cash {d.delta_cash <= 0 ? '−' : '+'}
        {inr(Math.abs(d.delta_cash))}
      </p>
      {d.changes.length === 0 ? (
        <p className="muted">Same actions as before.</p>
      ) : (
        <table className="table compact">
          <thead>
            <tr>
              <th>SKU</th>
              <th>Change</th>
              <th>Before</th>
              <th>Now</th>
              <th className="num">Cash Δ</th>
            </tr>
          </thead>
          <tbody>
            {d.changes.map((c) => (
              <tr key={c.sku_id}>
                <td>
                  <a href={`#/sku/${c.sku_id}`}>{c.sku_id}</a> <span className="muted small">{c.sku_name}</span>
                </td>
                <td className={`change-${c.change}`}>{c.change}</td>
                <td>{c.before}</td>
                <td>{c.after}</td>
                <td className="num">{inr(c.cash_delta)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Card>
  );
}

function Frontier({ plan, onPick }: { plan: Plan; onPick: (b: number) => void }) {
  const rows = plan.summary.frontier ?? [];
  if (!rows.length) return null;
  const data = rows.map((r) => ({ ...r, label: `₹${Math.round(r.budget / 1000)}k` }));
  const cur = rows.find((r) => r.is_current);
  return (
    <Card title="Budget frontier: what each extra rupee buys">
      <ResponsiveContainer width="100%" height={240}>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="label" />
          <YAxis tickFormatter={(v: number) => `₹${Math.round(v / 1000)}k`} width={60} />
          <Tooltip formatter={(v) => inr(Number(v))} />
          <Legend />
          <Line type="monotone" dataKey="eval_loss" name="Simulated loss (evaluation world)" stroke="#2563eb" strokeWidth={2} isAnimationActive={false} />
          <Line type="monotone" dataKey="planning_loss" name="Expected loss (planning forecast)" stroke="#9ca3af" strokeDasharray="4 4" isAnimationActive={false} />
          {cur && <ReferenceLine x={`₹${Math.round(cur.budget / 1000)}k`} stroke="#5b21b6" label="current" />}
        </LineChart>
      </ResponsiveContainer>
      <table className="table compact">
        <thead>
          <tr>
            <th className="num">Budget</th>
            <th className="num">Cash used</th>
            <th className="num">Expected loss</th>
            <th className="num">Simulated loss</th>
            <th className="num">Fill rate</th>
            <th className="num">Hero fill</th>
            <th className="num">Actions</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.budget} className={r.is_current ? 'overridden' : ''}>
              <td className="num">{inr(r.budget)}</td>
              <td className="num">{inr(r.cash_used)}</td>
              <td className="num">{inr(r.planning_loss)}</td>
              <td className="num strong">{inr(r.eval_loss)}</td>
              <td className="num">{pct(r.eval_fill_rate, 1)}</td>
              <td className="num">{pct(r.eval_hero_fill_rate, 1)}</td>
              <td className="num">{r.n_actions}</td>
              <td>
                {!r.is_current && r.budget > 0 && (
                  <button className="link" onClick={() => onPick(r.budget)}>
                    use
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="muted small">
        One optimisation, solved at every budget. The gap between the curves is forecast error: the planning forecast
        vs the simulated actual demand.
      </p>
    </Card>
  );
}

function DistCell({ d, money = true }: { d: Dist; money?: boolean }) {
  const f = (v: number) => (money ? inr(v) : pct(v, 1));
  return (
    <td className="num dist">
      {f(d.mean)}
      <div className="range">
        {f(d.p5)} – {f(d.p95)}
      </div>
    </td>
  );
}

function RobustnessCard({ r, onRetry }: { r: Robustness | undefined; onRetry: () => void }) {
  if (!r) return null;
  if (r.status !== 'done')
    return (
      <Card title="Robustness check">
        {r.status === 'failed' ? (
          <p className="error-inline">
            Evaluation failed: {r.error}{' '}
            <button className="link" onClick={onRetry}>
              retry
            </button>
          </p>
        ) : (
          <Spinner label="Replaying the plan in Monte Carlo worlds and weekly re-plans…" />
        )}
      </Card>
    );
  const mc = r.monte_carlo!;
  const roll = r.rolling!;
  const ab = r.signal_ablation!;
  return (
    <div className="grid2">
      <Card title={`Monte Carlo: ${mc.n_worlds} simulated futures`}>
        <table className="table compact">
          <thead>
            <tr>
              <th>Policy</th>
              <th className="num">Loss (mean, p5–p95)</th>
              <th className="num">Fill rate</th>
              <th className="num">Hero fill</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(mc.policies).map(([k, v]) => (
              <tr key={k}>
                <td>{POLICY_LABEL[k] ?? k}</td>
                <DistCell d={v.total_loss} />
                <DistCell d={v.fill_rate} money={false} />
                <DistCell d={v.hero_fill_rate} money={false} />
              </tr>
            ))}
          </tbody>
        </table>
        <ul className="bullets small">
          {Object.entries(mc.savings).map(([k, v]) => (
            <li key={k}>
              vs {POLICY_LABEL[k.replace('plan_vs_', '')] ?? k}: saves <strong>{inr(v.mean)}</strong> on average (
              {inr(v.p5)} to {inr(v.p95)}), better in <strong>{pct(v.win_rate)}</strong> of futures
            </li>
          ))}
        </ul>
        <p className="muted small">{r.notes}</p>
      </Card>
      <Card title="Rolling re-plan (weekly reviews)">
        <table className="table compact">
          <thead>
            <tr>
              <th>Policy</th>
              <th className="num">Loss (actual world)</th>
              <th className="num">Loss ({roll.n_mc_worlds} futures)</th>
              <th className="num">Fill</th>
              <th className="num">Orders</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(roll.actual_world).map(([k, v]) => (
              <tr key={k}>
                <td>{POLICY_LABEL[k] ?? k}</td>
                <td className="num strong">{inr(v.total_loss)}</td>
                {roll.mc[k] ? <DistCell d={roll.mc[k].total_loss} /> : <td />}
                <td className="num">{pct(v.fill_rate, 1)}</td>
                <td className="num">{v.orders_placed}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <ul className="bullets small">
          {Object.entries(roll.comparisons).map(([k, v]) => (
            <li key={k}>
              {POLICY_LABEL['replan_weekly']} vs {POLICY_LABEL[k.replace('replan_weekly_vs_', '')]}: {inr(v.mean)} lower
              loss on average, better in {pct(v.win_rate)} of futures
            </li>
          ))}
        </ul>
        <p className="muted small">{roll.notes}</p>
        <h3 className="small">Value of confirmed signals</h3>
        {ab.status === 'done' && ab.actual_world ? (
          <p className="small">
            With signals {inr(ab.actual_world.with_signals)} vs without {inr(ab.actual_world.without_signals)}:{' '}
            <strong>{inr(ab.actual_world.value_of_signals)}</strong> saved ({ab.n_changed_skus} SKUs decided
            differently)
            {ab.monte_carlo && (
              <>
                ; across futures {inr(ab.monte_carlo.mean)} on average, better in {pct(ab.monte_carlo.win_rate)}
              </>
            )}
            .
          </p>
        ) : (
          <p className="muted small">{ab.note ?? ab.status}</p>
        )}
      </Card>
    </div>
  );
}

function OptionsCard({ plan, onUse, busy }: { plan: Plan; onUse: (k: Strategy) => void; busy: boolean }) {
  const opts = plan.summary.options;
  if (!opts?.length) return null;
  const rob = plan.summary.robustness;
  const ev = rob?.status === 'done' ? rob.options : undefined;
  const best = ev?.best_service ?? {};
  const tag = (k: Strategy) =>
    (['actual', 'calm', 'shock'] as const)
      .filter((g) => best[g] === k)
      .map((g) => (
        <Badge key={g} kind="ok" title={`Lowest loss incl. service penalty in ${g === 'actual' ? 'the actual world' : `${g} worlds`}`}>
          best {g === 'actual' ? 'actual' : g === 'calm' ? 'calm' : 'under shocks'}
        </Badge>
      ));
  return (
    <Card title="Plan options: when to spend the budget">
      <p className="muted small">
        Same budget, same forecasts, four ways to use it. <em>Day-0 loss</em> assumes nothing else is bought this month;
        the <em>with weekly reviews</em> columns re-plan the remaining cash on days 7, 14 and 21 (scheduled tranches go
        back to that review, which can re-order, resize or redirect them) and are paired: every option sees the same
        futures.
      </p>
      <table className="table compact">
        <thead>
          <tr>
            <th>Option</th>
            <th className="num">Paid today</th>
            <th className="num">Scheduled</th>
            <th className="num">Held back</th>
            <th className="num">Day-0 loss (actual)</th>
            <th className="num">Weekly reviews: actual</th>
            <th className="num">Calm futures{ev ? ` (${ev.n_calm})` : ''}</th>
            <th className="num">Shock futures{ev ? ` (${ev.n_shock})` : ''}</th>
            <th className="num">Fill / hero</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {opts.map((o) => {
            const e = ev?.options[o.key];
            const vs = e?.vs_commit_now;
            return (
              <tr key={o.key} className={o.is_current ? 'overridden' : ''}>
                <td>
                  <strong>{o.label}</strong> {o.is_current && <Badge kind="neutral">current</Badge>} {tag(o.key)}
                  <div className="muted small">{o.description}</div>
                  {o.n_staggered > 0 && <div className="small">{o.n_staggered} buys split into two tranches</div>}
                  {o.key === 'lean' && <div className="small">operates on {inr(o.operating_budget)}</div>}
                </td>
                <td className="num strong">{inr(o.cash_today)}</td>
                <td className="num">{o.cash_scheduled ? inr(o.cash_scheduled) : '–'}</td>
                <td className="num">{inr(o.cash_uncommitted)}</td>
                <td className="num">
                  {inr(o.eval_loss_no_review)}
                  <div className="range">plan view {inr(o.planning_loss)}</div>
                </td>
                <td className="num">{e ? inr(e.actual.total_loss) : <Spinner />}</td>
                <td className="num">
                  {e?.calm ? (
                    <>
                      {inr(e.calm.total_loss.mean)}
                      {vs?.calm && (
                        <div className="range">
                          {inr(Math.abs(vs.calm.mean))} {vs.calm.mean >= 0 ? 'better' : 'worse'} than commit now · lower loss in{' '}
                          {pct(vs.calm.win_rate)}
                        </div>
                      )}
                    </>
                  ) : (
                    '–'
                  )}
                </td>
                <td className="num">
                  {e?.shock ? (
                    <>
                      {inr(e.shock.total_loss.mean)}
                      {vs?.shock && (
                        <div className="range">
                          {inr(Math.abs(vs.shock.mean))} {vs.shock.mean >= 0 ? 'better' : 'worse'} than commit now · lower loss in{' '}
                          {pct(vs.shock.win_rate)}
                        </div>
                      )}
                    </>
                  ) : (
                    '–'
                  )}
                </td>
                <td className="num">
                  {e ? `${pct(e.actual.fill_rate, 1)} / ${pct(e.actual.hero_fill_rate, 1)}` : `${pct(o.eval_fill_rate, 1)} / ${pct(o.eval_hero_fill_rate, 1)}`}
                </td>
                <td>
                  {!o.is_current && plan.status !== 'approved' && (
                    <button className="ghost" disabled={busy} onClick={() => onUse(o.key)}>
                      Use this option
                    </button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {ev && <p className="muted small">{ev.notes}</p>}
    </Card>
  );
}

export default function PlanPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [budget, setBudget] = useState<string>('');
  const [strategy, setStrategy] = useState<Strategy>('commit_now');
  const [busy, setBusy] = useState<'' | 'plan' | 'approve' | 'override'>('');
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [editing, setEditing] = useState(false);
  const [edits, setEdits] = useState<Record<string, Edit>>({});

  const load = useCallback(async () => {
    try {
      const [o, p] = await Promise.all([api.overview(), api.latestPlan()]);
      setOverview(o);
      setPlan(p);
      setBudget((b) => b || String(p?.budget ?? o.default_budget));
      if (p?.summary.strategy) setStrategy(p.summary.strategy);
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const rstatus = plan?.summary.robustness?.status;
  const planId = plan?.id;
  useEffect(() => {
    if (rstatus !== 'queued' || planId === undefined) return;
    const t = setTimeout(async () => {
      try {
        const p = await api.plan(planId);
        setPlan((cur) => (cur && cur.id === p.id ? p : cur));
      } catch {
        /* next poll */
      }
    }, 2500);
    return () => clearTimeout(t);
  }, [rstatus, planId, plan]);

  const generate = async (b0?: number, strat?: Strategy) => {
    const b = b0 ?? Number(budget);
    const st = strat ?? strategy;
    if (!Number.isFinite(b) || b <= 0) {
      setError('Budget must be a positive number.');
      return;
    }
    if (b0 !== undefined) setBudget(String(b0));
    if (strat !== undefined) setStrategy(strat);
    setBusy('plan');
    setError(null);
    setEditing(false);
    setEdits({});
    try {
      setPlan(await api.createPlan(b, st));
      setOverview(await api.overview());
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy('');
    }
  };

  const approve = async () => {
    if (!plan) return;
    setBusy('approve');
    setError(null);
    try {
      setPlan(await api.approvePlan(plan.id));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy('');
    }
  };

  const applyEdits = async () => {
    if (!plan) return;
    const items: OverrideItem[] = [];
    for (const a of plan.actions) {
      const e = edits[a.sku_id];
      if (!e) continue;
      if (e.skip) {
        items.push({ sku_id: a.sku_id, skip: true });
        continue;
      }
      const it: OverrideItem = { sku_id: a.sku_id };
      if (e.qty !== undefined && Number(e.qty) !== a.order_qty) it.order_qty = Number(e.qty);
      const oldPct = Math.round((a.discount_pct ?? 0) * 100);
      if (e.pct !== undefined && Number(e.pct) !== oldPct) it.discount_pct = Number(e.pct) / 100;
      if (it.order_qty !== undefined || it.discount_pct !== undefined) items.push(it);
    }
    if (!items.length) {
      setEditing(false);
      return;
    }
    setBusy('override');
    setError(null);
    try {
      setPlan(await api.overridePlan(plan.id, items));
      setEditing(false);
      setEdits({});
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setBusy('');
    }
  };

  const retryEval = async () => {
    if (!plan) return;
    await fetch(`/api/plans/${plan.id}/evaluate`, { method: 'POST' });
    setPlan(await api.plan(plan.id));
  };

  if (!loaded) return <Spinner label="Loading…" />;

  const s = plan?.summary;
  const stale =
    plan && overview?.forecasts.latest && new Date(overview.forecasts.latest) > new Date(plan.created_at);
  const fm = s?.forecast_methods ?? {};
  const heroes = new Set(s?.service?.hero_skus ?? []);
  const card = s?.scorecard;

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Recovery Plan</h1>
          <p className="muted">
            Data as of <strong>{overview?.as_of}</strong> · {overview?.n_skus} SKUs · 30-day horizon
          </p>
        </div>
        <div className="toolbar">
          <label>
            Cash budget (₹){' '}
            <input
              type="number"
              min={0}
              step={5000}
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              className="w-num"
            />
          </label>
          <label>
            Option{' '}
            <select value={strategy} onChange={(e) => setStrategy(e.target.value as Strategy)}>
              {(Object.keys(STRATEGY_LABEL) as Strategy[]).map((k) => (
                <option key={k} value={k}>
                  {STRATEGY_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
          <button className="primary" onClick={() => generate()} disabled={!!busy}>
            {busy === 'plan' ? <Spinner label="Planning…" /> : plan ? 'Re-plan' : 'Generate plan'}
          </button>
        </div>
      </div>
      <ErrorBox error={error} />

      {overview && overview.pending_signals > 0 && (
        <div className="notice">
          {overview.pending_signals} extracted signal(s) waiting for review in the{' '}
          <a href="#/inbox">Signal Inbox</a>. Only confirmed signals affect forecasts.
        </div>
      )}
      {overview && overview.active_jobs.some((j) => j.kind !== 'evaluate_plan') && (
        <div className="notice">
          <Spinner /> Background job(s) running (forecast refresh). Re-plan when done.{' '}
          <button className="link" onClick={load}>
            Refresh
          </button>
        </div>
      )}
      {stale && (
        <div className="notice warn">
          Forecasts changed after this plan was created ({new Date(overview!.forecasts.latest!).toLocaleString()}).
          Re-plan to use them.
        </div>
      )}

      {!plan && (
        <Card>
          <p>
            No plan yet. Set a budget and click <strong>Generate plan</strong>.
          </p>
        </Card>
      )}

      {plan && s && card && (
        <>
          <div className="kpis">
            <Kpi
              label="Cash committed"
              value={inr(s.cash_used)}
              sub={
                <>
                  of {inr(s.budget)} budget · {inr(s.cash_remaining)} left
                  {!!s.cash_scheduled && (
                    <>
                      {' '}
                      · {inr(s.cash_today)} today, {inr(s.cash_scheduled)} scheduled
                    </>
                  )}
                  {s.strategy_label && <> · option: {s.strategy_label}</>}
                </>
              }
            />
            <Kpi
              label="Expected 30-day loss"
              value={
                <>
                  {inr(s.expected_loss_no_action)} → {inr(s.expected_loss_plan)}
                </>
              }
              sub="no action → with plan (planning forecast)"
            />
            <Kpi
              label="Savings vs reorder rules"
              tone={card.savings_vs_tuned >= 0 ? 'good' : 'bad'}
              value={inr(card.savings_vs_tuned)}
              sub={
                <>
                  vs best-tuned rule ({s.tuned_baseline.safety_days}d safety, {s.tuned_baseline.cover_days}d cover);{' '}
                  {inr(card.savings)} vs static rule · <a href="#/scenario">details</a>
                </>
              }
            />
            <Kpi
              label="Fill rate (simulated)"
              value={pct(s.service.plan_fill_rate, 1)}
              sub={
                <>
                  hero SKUs {pct(s.service.plan_hero_fill_rate, 1)} · rule {pct(s.service.baseline_fill_rate, 1)} / hero{' '}
                  {pct(s.service.baseline_hero_fill_rate, 1)}
                </>
              }
            />
            <Kpi
              label="Forecasts"
              value={`${fm.hybrid ?? 0} hybrid · ${fm.statistical ?? 0} statistical`}
              sub={overview ? `${overview.forecasts.fresh}/${overview.forecasts.total} fresh · ${s.skus_at_risk} SKUs at risk` : undefined}
            />
          </div>

          <Card
            title={
              <>
                Why this plan <MethodBadge method={plan.explanation.method} reason={plan.explanation.error} />
              </>
            }
          >
            {plan.explanation.stale_note && <div className="notice warn small">{plan.explanation.stale_note}</div>}
            <p>{plan.explanation.summary}</p>
            <ul className="bullets">
              {plan.explanation.bullets.map((b, i) => (
                <li key={i}>
                  {b.text}{' '}
                  {b.cites.map((c) => (
                    <RecordChip key={c} id={c} />
                  ))}
                </li>
              ))}
            </ul>
            {plan.explanation.error && (
              <p className="muted small">Model explanation rejected: {plan.explanation.error}</p>
            )}
          </Card>

          {s.diff && <DiffCard d={s.diff} />}

          <Card
            title={`Ranked actions (plan #${plan.id}${s.revision ? `, edited ×${s.revision}` : ''})`}
            actions={
              plan.status === 'approved' ? (
                <>
                  <Badge kind="ok">approved {plan.approved_at && new Date(plan.approved_at).toLocaleString()}</Badge>
                  <a className="button" href={`/api/plans/${plan.id}/export/po`}>
                    Draft PO CSV ({plan.exports?.po_lines ?? 0})
                  </a>
                  <a className="button" href={`/api/plans/${plan.id}/export/markdowns`}>
                    Markdown CSV ({plan.exports?.markdown_lines ?? 0})
                  </a>
                </>
              ) : editing ? (
                <>
                  <button
                    className="ghost"
                    onClick={() => {
                      setEditing(false);
                      setEdits({});
                    }}
                    disabled={!!busy}
                  >
                    Cancel
                  </button>
                  <button className="primary" onClick={applyEdits} disabled={!!busy}>
                    {busy === 'override' ? <Spinner label="Re-scoring…" /> : 'Apply overrides'}
                  </button>
                </>
              ) : (
                <>
                  <button className="ghost" onClick={() => setEditing(true)} disabled={!!busy || !plan.actions.length}>
                    Edit plan
                  </button>
                  <button className="primary" onClick={approve} disabled={!!busy || plan.actions.length === 0}>
                    {busy === 'approve' ? <Spinner label="Approving…" /> : 'Approve & export CSVs'}
                  </button>
                </>
              )
            }
          >
            {plan.actions.length === 0 ? (
              <p className="muted">No action beats deferring within this budget.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>SKU</th>
                    <th>Action</th>
                    <th>Detail</th>
                    <th className="num">Cash</th>
                    <th className="num">Loss if deferred</th>
                    <th className="num">Loss with action</th>
                    <th className="num">Loss avoided</th>
                    <th>Forecast</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {plan.actions.map((a) => (
                    <ActionRow
                      key={a.id}
                      a={a}
                      hero={heroes.has(a.sku_id)}
                      editing={editing}
                      edit={edits[a.sku_id]}
                      onEdit={(e) => setEdits((cur) => ({ ...cur, [a.sku_id]: e }))}
                    />
                  ))}
                </tbody>
              </table>
            )}
            {(s.overridden_removed ?? []).length > 0 && (
              <p className="small">
                Skipped by operator:{' '}
                {s.overridden_removed!.map((x) => (
                  <span key={x.sku_id} className="chip" title={`Optimiser suggested: ${x.original}`}>
                    {x.sku_id}
                  </span>
                ))}
              </p>
            )}
            <p className="muted small">
              {editing
                ? 'Quantities are rounded to MOQ / case pack; discounts 5–60% apply to the soonest-expiring lot. The edited plan is re-scored with the same simulator and must fit the budget.'
                : 'Click a row for reasons and the records behind it. Loss = lost margin + expiry write-off + markdown cost + cannibalised margin + expedite premium, averaged over demand paths and supplier-delay cases.'}
            </p>
          </Card>

          <OptionsCard plan={plan} busy={!!busy} onUse={(k) => generate(plan.budget, k)} />

          <RobustnessCard r={s.robustness} onRetry={retryEval} />

          <Frontier plan={plan} onPick={(b) => generate(b)} />

          <div className="grid2">
            <Card title="Active alerts">
              <AlertList alerts={overview?.alerts ?? []} />
            </Card>
            <Card title="Reorder-rule baselines (for comparison)">
              <p className="muted small">
                Static rule: reorder point on stated lead time + 7 days safety, order up to 30 days of cover. Tuned
                rule: the same rule with safety/cover chosen from {s.tuned_baseline.grid} to minimise expected loss
                with today's information. Neither marks down, expedites or uses signals; same budget.
              </p>
              <table className="table compact">
                <thead>
                  <tr>
                    <th />
                    <th className="num">Loss</th>
                    <th className="num">Fill</th>
                    <th className="num">Hero fill</th>
                    <th className="num">Expired units</th>
                  </tr>
                </thead>
                <tbody>
                  {(
                    [
                      ['Plan', card.plan, s.service.plan_hero_fill_rate],
                      ['Tuned rule', card.tuned_baseline, s.service.tuned_hero_fill_rate],
                      ['Static rule', card.baseline, s.service.baseline_hero_fill_rate],
                    ] as const
                  ).map(([n, t, h]) => (
                    <tr key={n}>
                      <td>{n}</td>
                      <td className="num strong">{inr(t.total_loss)}</td>
                      <td className="num">{pct(t.fill_rate, 1)}</td>
                      <td className="num">{pct(h, 1)}</td>
                      <td className="num">{num(t.expired_units)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="small">
                Static rule orders:{' '}
                {plan.baseline_actions.length === 0
                  ? 'none'
                  : plan.baseline_actions.map((b) => `${b.sku_id} ×${b.order_qty}`).join(', ')}
                <br />
                Tuned rule orders:{' '}
                {s.tuned_baseline.actions.length === 0
                  ? 'none'
                  : s.tuned_baseline.actions.map((b) => `${b.sku_id} ×${b.order_qty}`).join(', ')}
              </p>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
