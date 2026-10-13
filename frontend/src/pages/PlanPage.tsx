import { useCallback, useEffect, useState } from 'react';
import { api, inr, num, pct } from '../api';
import type { Overview, Plan, PlanAction } from '../api';
import { AlertList, Badge, Card, ErrorBox, Kpi, MethodBadge, RecordChip, Spinner } from '../ui';

const KIND: Record<string, [string, string]> = {
  standard_po: ['ok', 'buy'],
  expedited_po: ['bad', 'expedite'],
  markdown: ['warn', 'markdown'],
  defer: ['neutral', 'defer'],
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

function ActionRow({ a }: { a: PlanAction }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <tr className="clickable" onClick={() => setOpen(!open)}>
        <td>{a.rank}</td>
        <td>
          <a href={`#/sku/${a.sku_id}`} onClick={(e) => e.stopPropagation()}>
            {a.sku_id}
          </a>
          <div className="muted small">{a.sku_name}</div>
        </td>
        <td>
          <KindBadges kind={a.kind} />
        </td>
        <td>{a.label}</td>
        <td className="num">{inr(a.cash_outlay)}</td>
        <td className="num">{inr(a.expected_loss_if_deferred)}</td>
        <td className="num">{inr(a.expected_loss_with_action)}</td>
        <td className="num strong">{inr(a.loss_avoided)}</td>
        <td>
          <MethodBadge method={a.forecast_method} />
        </td>
        <td className="muted">{open ? '▾' : '▸'}</td>
      </tr>
      {open && (
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

export default function PlanPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [budget, setBudget] = useState<string>('');
  const [busy, setBusy] = useState<'' | 'plan' | 'approve'>('');
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const [o, p] = await Promise.all([api.overview(), api.latestPlan()]);
      setOverview(o);
      setPlan(p);
      setBudget((b) => b || String(p?.budget ?? o.default_budget));
    } catch (e) {
      setError(String((e as Error).message));
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const generate = async () => {
    const b = Number(budget);
    if (!Number.isFinite(b) || b <= 0) {
      setError('Budget must be a positive number.');
      return;
    }
    setBusy('plan');
    setError(null);
    try {
      setPlan(await api.createPlan(b));
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

  if (!loaded) return <Spinner label="Loading…" />;

  const s = plan?.summary;
  const stale =
    plan && overview?.forecasts.latest && new Date(overview.forecasts.latest) > new Date(plan.created_at);
  const fm = s?.forecast_methods ?? {};

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
          <button className="primary" onClick={generate} disabled={!!busy}>
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
      {overview && overview.active_jobs.length > 0 && (
        <div className="notice">
          <Spinner /> {overview.active_jobs.length} background job(s) running (forecast refresh). Re-plan when done.{' '}
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

      {plan && s && (
        <>
          <div className="kpis">
            <Kpi
              label="Cash used"
              value={inr(s.cash_used)}
              sub={`of ${inr(s.budget)} budget · ${inr(s.cash_remaining)} left`}
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
              label="Savings vs rule baseline"
              tone={s.scorecard.savings >= 0 ? 'good' : 'bad'}
              value={inr(s.scorecard.savings)}
              sub={
                <>
                  simulated; baseline {inr(s.scorecard.baseline.total_loss)} vs plan {inr(s.scorecard.plan.total_loss)}{' '}
                  · <a href="#/scenario">details</a>
                </>
              }
            />
            <Kpi label="SKUs at risk" value={num(s.skus_at_risk)} sub={`${s.n_actions} actions recommended`} />
            <Kpi
              label="Forecasts"
              value={`${fm.llm ?? 0} LLM · ${fm.fallback ?? 0} fallback`}
              sub={overview ? `${overview.forecasts.fresh}/${overview.forecasts.total} fresh` : undefined}
            />
          </div>

          <Card
            title={
              <>
                Why this plan <MethodBadge method={plan.explanation.method} reason={plan.explanation.error} />
              </>
            }
          >
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
              <p className="muted small">LLM explanation rejected: {plan.explanation.error}</p>
            )}
          </Card>

          <Card
            title={`Ranked actions (plan #${plan.id})`}
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
              ) : (
                <button className="primary" onClick={approve} disabled={!!busy || plan.actions.length === 0}>
                  {busy === 'approve' ? <Spinner label="Approving…" /> : 'Approve & export CSVs'}
                </button>
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
                    <ActionRow key={a.id} a={a} />
                  ))}
                </tbody>
              </table>
            )}
            <p className="muted small">
              Click a row for reasons and the records behind it. Loss = lost margin + expiry write-off + markdown cost
              + expedite premium, averaged over low/base/high demand paths.
            </p>
          </Card>

          <div className="grid2">
            <Card title="Active alerts">
              <AlertList alerts={overview?.alerts ?? []} />
            </Card>
            <Card title="Rule-based baseline (for comparison)">
              <p className="muted small">
                Reorder-point rule: order up to cover when position falls below the reorder point; no markdowns, no
                expedites, no signals, same budget.
              </p>
              {plan.baseline_actions.length === 0 ? (
                <p className="muted">Baseline places no orders.</p>
              ) : (
                <table className="table compact">
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>Action</th>
                      <th className="num">Qty</th>
                      <th className="num">Cash</th>
                    </tr>
                  </thead>
                  <tbody>
                    {plan.baseline_actions.map((b, i) => (
                      <tr key={i}>
                        <td>
                          <a href={`#/sku/${b.sku_id}`}>{b.sku_id}</a>
                        </td>
                        <td>
                          <KindBadges kind={b.kind} />
                        </td>
                        <td className="num">{num(b.order_qty)}</td>
                        <td className="num">{inr(b.cash_outlay)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
              <p className="small">
                Simulated stockout SKU-days: plan {num(s.scorecard.plan.stockout_sku_days)} vs baseline{' '}
                {num(s.scorecard.baseline.stockout_sku_days)} · expired units {num(s.scorecard.plan.expired_units)} vs{' '}
                {num(s.scorecard.baseline.expired_units)} · budget use {pct(s.cash_used / s.budget)}
              </p>
            </Card>
          </div>
        </>
      )}
    </div>
  );
}
