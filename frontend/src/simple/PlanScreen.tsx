import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Overview, Plan, Strategy } from '../api';
import { Back, Problem, Working } from './parts';
import { actionSteps, actionWhy, kindIcon, rupees } from './plain';

const TIP: Partial<Record<Strategy, string>> = {
  reserve: 'keep some money aside for surprises',
  staggered: 'buy in two smaller steps',
  lean: 'spend a little less now',
};

const digits = (s: string) => Number(s.replace(/[^\d]/g, ''));
const lakh = (v: number) => (v >= 1e5 ? ` (${Number((v / 1e5).toFixed(2))} lakh)` : '');

export default function PlanScreen() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [budget, setBudget] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<'' | 'plan' | 'approve'>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.overview(), api.latestPlan()])
      .then(([o, p]) => {
        setOverview(o);
        setPlan(p);
        setBudget(Math.round(p?.budget ?? o.default_budget).toLocaleString('en-IN'));
      })
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoaded(true));
  }, []);

  // The "what if things go wrong" test runs in the background after a plan is made.
  const rstatus = plan?.summary.robustness?.status;
  const planId = plan?.id;
  useEffect(() => {
    if (rstatus !== 'queued' || planId === undefined) return;
    const t = setTimeout(async () => {
      try {
        const p = await api.plan(planId);
        setPlan((cur) => (cur && cur.id === p.id ? p : cur));
      } catch {
        /* try again next tick */
      }
    }, 4000);
    return () => clearTimeout(t);
  }, [rstatus, planId, plan]);

  const make = async (strategy: Strategy = 'commit_now') => {
    const b = digits(budget);
    if (!b) {
      setError('Please type how much money you can spend, for example 10,00,000.');
      return;
    }
    setBusy('plan');
    setError(null);
    try {
      setPlan(await api.createPlan(b, strategy));
      setOverview(await api.overview());
    } catch (e) {
      setError((e as Error).message);
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
      setError((e as Error).message);
    } finally {
      setBusy('');
    }
  };

  if (!loaded) return <Working text="Loading…" />;

  const s = plan?.summary;
  const stale =
    plan && overview?.forecasts.latest && new Date(overview.forecasts.latest) > new Date(plan.created_at);
  const strategy = s?.strategy ?? 'commit_now';
  const best = s?.robustness?.status === 'done' ? s.robustness.options?.best?.shock : undefined;
  const tip = plan?.status === 'draft' && strategy === 'commit_now' && best && TIP[best] ? best : undefined;
  const later = s && s.cash_today !== undefined ? s.cash_used - s.cash_today : 0;
  const savings = s?.scorecard.savings ?? 0;

  return (
    <div className="s-screen">
      <Back />
      <h1>Make my plan</h1>
      <Problem error={error} />

      {overview && overview.pending_signals > 0 && (
        <p className="s-notice">
          ✉️ {overview.pending_signals === 1 ? '1 thing from a message needs' : `${overview.pending_signals} things from messages need`}{' '}
          your yes or no first. <a href="#/s/message">Check now</a>
        </p>
      )}

      <label className="s-label" htmlFor="s-budget">
        How much money can you spend?
      </label>
      <div className="s-row">
        <div className="s-money">
          <span>₹</span>
          <input
            id="s-budget"
            inputMode="numeric"
            value={budget}
            onChange={(e) => setBudget(e.target.value)}
            onBlur={() => digits(budget) && setBudget(digits(budget).toLocaleString('en-IN'))}
            onKeyDown={(e) => e.key === 'Enter' && !busy && make()}
          />
        </div>
        <button className="s-btn s-primary" onClick={() => make()} disabled={!!busy}>
          {plan ? 'Make a new plan' : 'Make my plan'}
        </button>
      </div>
      {digits(budget) > 0 && <p className="s-muted">That is {rupees(digits(budget))}{lakh(digits(budget))}.</p>}

      {busy === 'plan' && <Working text="Making your plan… this can take up to a minute." />}

      {plan && s && busy !== 'plan' && (
        <section className="s-section">
          {stale && (
            <p className="s-notice">
              New information arrived after this plan was made. Press “Make a new plan” to use it.
            </p>
          )}

          <div className="s-summary">
            <p className="s-big">
              Spend <strong>{rupees(s.cash_used)}</strong> of your {rupees(plan.budget)}.
            </p>
            {later > 1 && (
              <p>
                Pay {rupees(s.cash_today)} now and {rupees(later)} later, only if it is still needed.
              </p>
            )}
            {savings > 0 && (
              <p className="s-good">
                This should save you about <strong>{rupees(savings)}</strong> compared with reordering the usual way.
              </p>
            )}
          </div>

          {plan.actions.length === 0 ? (
            <p className="s-lead">Good news: nothing needs to be done right now.</p>
          ) : (
            <>
              <h2>
                Do {plan.actions.length === 1 ? 'this 1 thing' : `these ${plan.actions.length} things`}:
              </h2>
              <ol className="s-actions">
                {plan.actions.map((a) => {
                  const why = actionWhy(a);
                  return (
                    <li key={a.id} className="s-card s-action">
                      <div className="s-action-icon" aria-hidden>
                        {kindIcon(a)}
                      </div>
                      <div className="s-action-body">
                        <div className="s-action-name">{a.sku_name}</div>
                        {actionSteps(a).map((t) => (
                          <div key={t}>{t}</div>
                        ))}
                        {why && <div className="s-muted">{why}</div>}
                      </div>
                      <div className="s-action-cost">{a.cash_outlay > 0 ? rupees(a.cash_outlay) : 'No cost'}</div>
                    </li>
                  );
                })}
              </ol>
            </>
          )}

          {tip && (
            <div className="s-tip">
              <p>
                💡 <strong>Tip:</strong> in our tests, it was safer to {TIP[tip]} in case deliveries are late or sales
                jump.
              </p>
              <button className="s-btn" onClick={() => make(tip)} disabled={!!busy}>
                Use this instead
              </button>
            </div>
          )}

          {plan.status === 'approved' ? (
            <div className="s-next s-approved">
              <p className="s-big">✓ Plan approved.</p>
              <p>Open these lists in Excel, or send them to your supplier yourself.</p>
              <div className="s-row">
                {(plan.exports?.po_lines ?? 0) > 0 && (
                  <a className="s-btn s-primary" href={`/api/plans/${plan.id}/export/po`}>
                    ⬇ Download order list
                  </a>
                )}
                {(plan.exports?.markdown_lines ?? 0) > 0 && (
                  <a className="s-btn" href={`/api/plans/${plan.id}/export/markdowns`}>
                    ⬇ Download sale list
                  </a>
                )}
              </div>
            </div>
          ) : (
            plan.actions.length > 0 && (
              <div className="s-next">
                {busy === 'approve' ? (
                  <Working text="Saving…" />
                ) : (
                  <button className="s-btn s-primary s-wide" onClick={approve} disabled={!!busy}>
                    ✓ Approve and get my order list
                  </button>
                )}
              </div>
            )
          )}

          <p className="s-muted">
            Want the charts and numbers behind this? <a href="#/plan">See full details</a>
          </p>
        </section>
      )}
    </div>
  );
}
