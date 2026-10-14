import { useEffect, useState } from 'react';
import { api } from '../api';
import type { Overview, Plan, Strategy } from '../api';
import { Back, Problem, Working } from './parts';
import { Icon } from './icons';
import { actionSteps, actionWhy, day, kindIcon, rupees } from './plain';

const CHOICES: { key: Strategy; title: string; text: string }[] = [
  { key: 'commit_now', title: 'Buy it all now', text: 'Everything is ordered today. The simplest choice.' },
  { key: 'staggered', title: 'Buy in two steps', text: 'Order part now, the rest in 1–2 weeks only if it is still needed.' },
  { key: 'reserve', title: 'Keep money aside', text: 'Spend part now and keep the rest for surprises. You choose how much.' },
  { key: 'lean', title: 'Spend less', text: 'Spend only what gives most of the savings.' },
];

const TIP: Partial<Record<Strategy, string>> = {
  reserve: 'keep some money aside for surprises',
  staggered: 'buy in two smaller steps',
  lean: 'spend a little less now',
};

const digits = (s: string) => Number(s.replace(/[^\d]/g, ''));
const lakh = (v: number) => (v >= 1e5 ? ` (${Number((v / 1e5).toFixed(2))} lakh)` : '');
const roundUp = (v: number) => Math.ceil(v / 10000) * 10000;
const short = (v: number) => (v >= 1e5 ? `₹${Number((v / 1e5).toFixed(1))} lakh` : rupees(v));

export default function PlanScreen() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [budget, setBudget] = useState('');
  const [choice, setChoice] = useState<Strategy>('commit_now');
  const [keepPct, setKeepPct] = useState(30);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState<'' | 'plan' | 'approve'>('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.overview(), api.latestPlan()])
      .then(([o, p]) => {
        setOverview(o);
        setPlan(p);
        setBudget(Math.round(p?.budget ?? o.default_budget).toLocaleString('en-IN'));
        if (p?.summary.strategy) setChoice(p.summary.strategy);
        if (p?.summary.reserve_share) setKeepPct(Math.round(p.summary.reserve_share * 100));
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

  const make = async (strategy: Strategy = choice, amount?: number) => {
    setChoice(strategy);
    if (amount) setBudget(amount.toLocaleString('en-IN'));
    const b = amount ?? digits(budget);
    if (!b) {
      setError('Please type how much money you can spend, for example 10,00,000.');
      return;
    }
    setBusy('plan');
    setError(null);
    try {
      setPlan(await api.createPlan(b, strategy, keepPct / 100));
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
  const guide = s?.budget_guide;
  // Edge case: the budget cannot buy even the cheapest useful order, so the plan can only hold sales.
  const tooSmall = !!(plan && guide?.smallest_buy && plan.budget < guide.smallest_buy);
  const free = !!s && s.cash_used < 1;
  const keptByChoice = (strategy === 'reserve' || strategy === 'lean') && !!guide && plan!.budget >= guide.full_need;
  const left = guide?.not_covered ?? [];
  const hasOrders = !!plan?.actions.some((a) => a.order_qty > 0 || a.later_qty);
  const hasSales = !!plan?.actions.some((a) => a.order_qty === 0 || a.kind.startsWith('markdown'));
  const tip =
    !tooSmall && plan?.status === 'draft' && strategy === 'commit_now' && best && TIP[best] ? best : undefined;
  const later = s && s.cash_today !== undefined ? s.cash_used - s.cash_today : 0;
  const savings = s?.scorecard.savings ?? 0;
  const aside = plan && s ? plan.budget - s.cash_used : 0;
  // Per-choice figures are only meaningful when they were computed for the budget typed now.
  const sameBudget = plan && Math.round(plan.budget) === digits(budget);
  const sameKeep = Math.round((s?.reserve_share ?? 0.3) * 100) === keepPct;
  const optionRow = (k: Strategy) =>
    sameBudget && !tooSmall && (k !== 'reserve' || sameKeep) ? s?.options?.find((o) => o.key === k) : undefined;
  const b = digits(budget);
  const typedTooSmall = b > 0 && !!guide?.smallest_buy && b < guide.smallest_buy;
  const keepAmount = Math.round((b * keepPct) / 100);
  const safest = sameBudget ? best : undefined;

  return (
    <div className="s-screen">
      <Back />
      <h1>Make my plan</h1>
      <Problem error={error} />

      {overview && overview.pending_signals > 0 && (
        <p className="s-notice">
          <Icon name="mail" size={20} />{' '}
          {overview.pending_signals === 1 ? '1 thing from a message needs' : `${overview.pending_signals} things from messages need`}{' '}
          your yes or no first. <a href="#/s/message">Check now</a>
        </p>
      )}

      <label className="s-label" htmlFor="s-budget">
        How much money can you spend?
      </label>
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
      {b > 0 && (
        <p className="s-muted">
          That is {rupees(b)}
          {lakh(b)}.
          {typedTooSmall && (
            <span className="s-warn-text">
              {' '}This is too little to buy stock. The smallest order that helps costs about {rupees(guide!.smallest_buy!)}.
            </span>
          )}
        </p>
      )}

      <fieldset className="s-choices">
        <legend className="s-label">How do you want to buy?</legend>
        {CHOICES.map((c) => {
          const row = optionRow(c.key);
          return (
            <label key={c.key} className={`s-choice${choice === c.key ? ' s-choice-on' : ''}`}>
              <input
                type="radio"
                name="s-strategy"
                value={c.key}
                checked={choice === c.key}
                onChange={() => setChoice(c.key)}
              />
              <span className="s-choice-title">
                {c.title}
                {safest === c.key && <span className="s-badge">Safest in our tests</span>}
              </span>
              <span className="s-choice-text">{c.text}</span>
              {!row && c.key === 'reserve' && b > 0 && !typedTooSmall && (
                <span className="s-choice-figure">
                  Up to {short(b - keepAmount)} now · {short(keepAmount)} kept
                </span>
              )}
              {row && (
                <span className="s-choice-figure">
                  {short(row.cash_today)} now
                  {row.cash_scheduled > 1 && ` · ${short(row.cash_scheduled)} later`}
                  {row.cash_uncommitted > 0.01 * digits(budget) && ` · ${short(row.cash_uncommitted)} kept`}
                </span>
              )}
            </label>
          );
        })}
        {choice === 'reserve' && (
          <div className="s-keep">
            <label htmlFor="s-keep-range" className="s-keep-label">
              How much do you want to keep aside?
            </label>
            <div className="s-keep-row">
              <input
                id="s-keep-range"
                type="range"
                min={10}
                max={70}
                step={5}
                value={keepPct}
                onChange={(e) => setKeepPct(Number(e.target.value))}
                aria-valuetext={`${keepPct} percent`}
              />
              <span className="s-keep-pct">{keepPct}%</span>
            </div>
            {b > 0 && (
              <p className="s-keep-sum">
                Spend up to <strong>{rupees(b - keepAmount)}</strong> now. Keep <strong>{rupees(keepAmount)}</strong>{' '}
                for surprises.
              </p>
            )}
          </div>
        )}
      </fieldset>

      <button className="s-btn s-primary s-wide" onClick={() => make()} disabled={!!busy}>
        {plan ? 'Make a new plan' : 'Make my plan'}
      </button>

      {busy === 'plan' && <Working text="Making your plan… this can take up to a minute." />}

      {plan && s && busy !== 'plan' && (
        <section className="s-section">
          {stale && (
            <p className="s-notice">
              New information arrived after this plan was made. Press “Make a new plan” to use it.
            </p>
          )}

          <div className={`s-summary${tooSmall ? ' s-summary-warn' : ''}`}>
            {tooSmall ? (
              <>
                <p className="s-big">{rupees(plan.budget)} is not enough to buy any stock.</p>
                <p>
                  The smallest order that helps costs about <strong>{rupees(guide!.smallest_buy!)}</strong>.
                </p>
              </>
            ) : free ? (
              <p className="s-big">This plan needs no money.</p>
            ) : (
              <p className="s-big">
                Spend <strong>{rupees(s.cash_used)}</strong> of your {rupees(plan.budget)}.
              </p>
            )}
            {later > 1 && (
              <p>
                Pay {rupees(s.cash_today)} now and {rupees(later)} later, only if it is still needed.
              </p>
            )}
            {!free && (strategy === 'reserve' || strategy === 'lean') && aside > 1 && (
              <p>Keep {rupees(aside)} aside. We'll look again at your next weekly check.</p>
            )}
            {savings > 0 &&
              (free ? (
                <p className="s-good">
                  The free steps below should still save you about <strong>{rupees(savings)}</strong>.
                </p>
              ) : (
                <p className="s-good">
                  This should save you about <strong>{rupees(savings)}</strong> compared with reordering the usual way.
                </p>
              ))}
          </div>

          {plan.actions.length === 0 ? (
            <p className="s-lead">Good news: nothing needs to be done right now.</p>
          ) : (
            <>
              <h2>
                {tooSmall ? 'You can still do ' : 'Do '}
                {plan.actions.length === 1 ? 'this 1 thing' : `these ${plan.actions.length} things`}
                {free ? '. They cost nothing:' : ':'}
              </h2>
              <ol className="s-actions">
                {plan.actions.map((a) => {
                  const why = actionWhy(a);
                  return (
                    <li key={a.id} className="s-card s-action">
                      <div className="s-action-icon" aria-hidden>
                        <Icon name={kindIcon(a)} size={26} />
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

          {left.length > 0 && (
            <div className="s-short">
              <p className="s-short-title">
                <Icon name="alert" size={22} />
                {keptByChoice
                  ? `Not bought yet: ${left.length} product${left.length > 1 ? 's' : ''}`
                  : `${left.length} product${left.length > 1 ? 's' : ''} still need${left.length > 1 ? '' : 's'} stock`}
              </p>
              <ul>
                {left.slice(0, 3).map((x) => (
                  <li key={x.sku_id}>
                    <strong>{x.sku_name}</strong>
                    {x.stockout_date && <span className="s-muted"> runs out around {day(x.stockout_date)}</span>}
                  </li>
                ))}
                {left.length > 3 && <li className="s-muted">and {left.length - 3} more</li>}
              </ul>
              {keptByChoice ? (
                <p>You can buy these at your weekly check with the money you kept aside.</p>
              ) : (
                <>
                  <p>
                    To cover everything, you would need about <strong>{short(roundUp(guide!.full_need))}</strong>.
                  </p>
                  {plan.status === 'draft' && (
                    <button className="s-btn" onClick={() => make(choice, roundUp(guide!.full_need))} disabled={!!busy}>
                      Make a plan with {short(roundUp(guide!.full_need))}
                    </button>
                  )}
                </>
              )}
            </div>
          )}

          {tip && (
            <div className="s-tip">
              <p>
                <Icon name="bulb" size={20} /> <strong>Tip:</strong> in our tests, it was safer to {TIP[tip]} in case deliveries are late or sales
                jump.
              </p>
              <button className="s-btn" onClick={() => make(tip)} disabled={!!busy}>
                Use this instead
              </button>
            </div>
          )}

          {plan.status === 'approved' ? (
            <div className="s-next s-approved">
              <p className="s-big">
                <Icon name="check" /> Plan approved.
              </p>
              <p>Open these lists in Excel, or send them to your supplier yourself.</p>
              <div className="s-row">
                {(plan.exports?.po_lines ?? 0) > 0 && (
                  <a className="s-btn s-primary" href={`/api/plans/${plan.id}/export/po`}>
                    <Icon name="download" size={20} /> Download order list
                  </a>
                )}
                {(plan.exports?.markdown_lines ?? 0) > 0 && (
                  <a className="s-btn" href={`/api/plans/${plan.id}/export/markdowns`}>
                    <Icon name="download" size={20} /> Download sale list
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
                    <Icon name="check" /> Approve and get my{' '}
                    {hasOrders && hasSales ? 'lists' : hasOrders ? 'order list' : 'sale list'}
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
