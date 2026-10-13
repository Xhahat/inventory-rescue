import { useEffect, useMemo, useState } from 'react';
import { Area, CartesianGrid, ComposedChart, Legend, Line, ResponsiveContainer, Tooltip, XAxis, YAxis, ReferenceLine } from 'recharts';
import { api, inr, num, pct, waitForJob } from '../api';
import type { Job, SkuDetail, SkuRow } from '../api';
import { AlertList, Badge, Card, ErrorBox, MethodBadge, PriorityBadge, Spinner } from '../ui';

function SkuList() {
  const [rows, setRows] = useState<SkuRow[] | null>(null);
  const [q, setQ] = useState('');
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.skus().then(setRows).catch((e) => setError(e.message));
  }, []);
  const shown = (rows ?? []).filter((r) =>
    `${r.sku_id} ${r.name} ${r.category} ${r.supplier_id}`.toLowerCase().includes(q.toLowerCase()),
  );
  return (
    <div className="page">
      <div className="page-head">
        <h1>SKUs</h1>
        <input placeholder="Filter by id, name, category, supplier…" value={q} onChange={(e) => setQ(e.target.value)} />
      </div>
      <ErrorBox error={error} />
      {!rows && !error && <Spinner label="Loading…" />}
      {rows && (
        <Card>
          <table className="table">
            <thead>
              <tr>
                <th>SKU</th>
                <th>Name</th>
                <th>Category</th>
                <th>Supplier</th>
                <th className="num">On hand</th>
                <th className="num">Open PO</th>
                <th className="num">Avg/day (28d)</th>
                <th className="num">Days of cover</th>
                <th>Forecast</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={r.sku_id} className="clickable" onClick={() => (window.location.hash = `#/sku/${r.sku_id}`)}>
                  <td>
                    <a href={`#/sku/${r.sku_id}`}>{r.sku_id}</a> <PriorityBadge priority={r.priority} overridden={r.priority_overridden} />
                  </td>
                  <td>{r.name}</td>
                  <td>{r.category}</td>
                  <td>{r.supplier_id}</td>
                  <td className="num">{num(r.on_hand)}</td>
                  <td className="num">{num(r.open_po_qty)}</td>
                  <td className="num">{num(r.avg_daily_28d, 1)}</td>
                  <td className={`num ${r.days_of_cover !== null && r.days_of_cover < 10 ? 'bad-text' : ''}`}>
                    {num(r.days_of_cover, 1)}
                  </td>
                  <td>
                    <MethodBadge method={r.forecast_method} /> {!r.forecast_fresh && <Badge kind="warn">stale</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

type ChartRow = { date: string; actual?: number; p50?: number; low?: number; band?: number; p10?: number; p90?: number };

function ForecastChart({ d }: { d: SkuDetail }) {
  const data = useMemo<ChartRow[]>(() => {
    const rows: ChartRow[] = d.history.map((h) => ({ date: h.date, actual: h.units }));
    for (const f of d.forecast.days) {
      rows.push({ date: f.date, p50: f.p50, low: f.p10, band: Math.max(0, f.p90 - f.p10), p10: f.p10, p90: f.p90 });
    }
    return rows;
  }, [d]);
  return (
    <ResponsiveContainer width="100%" height={320}>
      <ComposedChart data={data} margin={{ top: 10, right: 20, left: 0, bottom: 0 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
        <XAxis dataKey="date" tickFormatter={(v: string) => v.slice(5)} minTickGap={24} fontSize={12} />
        <YAxis fontSize={12} />
        <Tooltip
          formatter={(v, name, item) => {
            const p = item?.payload as ChartRow | undefined;
            if (name === 'p10–p90 band' && p) return [`${num(p.p10, 1)} – ${num(p.p90, 1)}`, name];
            return [num(Number(v), 1), name];
          }}
        />
        <Legend />
        <Area dataKey="low" stackId="b" stroke="none" fill="transparent" legendType="none" isAnimationActive={false} name="low" tooltipType="none" />
        <Area dataKey="band" stackId="b" stroke="none" fill="#93c5fd" fillOpacity={0.45} name="p10–p90 band" isAnimationActive={false} />
        <Line dataKey="actual" stroke="#111827" dot={false} strokeWidth={1.5} name="Actual units" isAnimationActive={false} />
        <Line dataKey="p50" stroke="#2563eb" dot={false} strokeWidth={2} name="Forecast p50" isAnimationActive={false} />
        <ReferenceLine x={d.as_of} stroke="#9ca3af" strokeDasharray="4 4" label={{ value: 'as of', fontSize: 11, fill: '#6b7280' }} />
      </ComposedChart>
    </ResponsiveContainer>
  );
}

function Detail({ skuId }: { skuId: string }) {
  const [d, setD] = useState<SkuDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);

  const load = () => api.sku(skuId).then(setD).catch((e) => setError(e.message));
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [skuId]);

  const refresh = async (force: boolean) => {
    setError(null);
    try {
      const j = await api.refreshForecasts([skuId], force);
      setJob(j);
      const done = await waitForJob(j.id, setJob);
      if (done.status === 'done') await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const setPriority = async (p: 'auto' | 'hero' | 'standard') => {
    setError(null);
    try {
      await api.setPriority(skuId, p);
      await load();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (error && !d) return <ErrorBox error={error} />;
  if (!d) return <Spinner label="Loading…" />;
  const sup = d.supplier;
  const rm = d.reorder;
  const running = job && (job.status === 'queued' || job.status === 'running');
  const fc30 = d.forecast.days.reduce((s, x) => s + x.p50, 0);

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <p className="muted small">
            <a href="#/sku">← all SKUs</a>
          </p>
          <h1>
            {d.sku_id} · {d.name}
          </h1>
          <p className="muted">
            {d.category} · cost {inr(d.unit_cost)} · price {inr(d.unit_price)} · case pack {d.case_pack} · MOQ{' '}
            {d.moq} · shelf life {d.shelf_life_days} d
          </p>
          <p className="small">
            Service priority{' '}
            <span className="segmented">
              {(['auto', 'hero', 'standard'] as const).map((p) => {
                const active = p === 'auto' ? !d.priority.overridden : d.priority.overridden && d.priority.priority === p;
                return (
                  <button key={p} className={active ? 'active' : ''} onClick={() => setPriority(p)} disabled={active}>
                    {p === 'auto' ? `auto (${d.priority.auto})` : p}
                  </button>
                );
              })}
            </span>{' '}
            <span className="muted">
              {d.priority.priority === 'hero' ? 'Hero' : 'Standard'}: each unit short costs an extra{' '}
              {inr(d.priority.goodwill_per_unit)} in the planner (lost repeat business). Re-plan to apply.
            </span>
          </p>
        </div>
        <div className="toolbar">
          <button onClick={() => refresh(false)} disabled={!!running}>
            {running ? <Spinner label="Refreshing…" /> : 'Refresh forecast'}
          </button>
          <button onClick={() => refresh(true)} disabled={!!running} title="Ignore caches and call the model again">
            Force
          </button>
        </div>
      </div>
      <ErrorBox error={error} />
      {job?.status === 'failed' && <ErrorBox error={job.error} />}

      <Card
        title={
          <>
            Demand: last 56 days and 30-day forecast{' '}
            <MethodBadge method={d.forecast.method} reason={d.forecast.fallback_reason} />{' '}
            {!d.forecast.fresh && <Badge kind="warn">stale</Badge>}
          </>
        }
      >
        <ForecastChart d={d} />
        <div className="grid2">
          <div>
            <h3>Forecast drivers</h3>
            {d.forecast.drivers.length ? (
              <ul>
                {d.forecast.drivers.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            ) : (
              <p className="muted">None reported.</p>
            )}
            {d.forecast.method !== 'hybrid' && d.forecast.fallback_reason && (
              <p className="muted small">Why no model adjustment: {d.forecast.fallback_reason}</p>
            )}
          </div>
          <div>
            <h3>Summary</h3>
            <p>
              30-day p50 demand: <strong>{num(fc30)}</strong> units
              <br />
              Forecast #{d.forecast.id ?? '—'}{' '}
              {d.forecast.created_at && <>created {new Date(d.forecast.created_at).toLocaleString()}</>}
            </p>
          </div>
        </div>
      </Card>

      <div className="grid2">
        <Card title="Inventory lots">
          {d.lots.length === 0 ? (
            <p className="muted">No stock on hand.</p>
          ) : (
            <table className="table compact">
              <thead>
                <tr>
                  <th>Lot</th>
                  <th className="num">Qty</th>
                  <th>Received</th>
                  <th>Expiry</th>
                  <th className="num">Days left</th>
                </tr>
              </thead>
              <tbody>
                {d.lots.map((l) => (
                  <tr key={l.lot_id}>
                    <td>{l.lot_id}</td>
                    <td className="num">{num(l.qty)}</td>
                    <td>{l.received_date}</td>
                    <td>{l.expiry_date}</td>
                    <td className={`num ${l.days_to_expiry <= 14 ? 'bad-text' : ''}`}>{l.days_to_expiry}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <h3>Open purchase orders</h3>
          {d.open_pos.length === 0 ? (
            <p className="muted">None.</p>
          ) : (
            <table className="table compact">
              <thead>
                <tr>
                  <th>PO</th>
                  <th className="num">Qty</th>
                  <th>Ordered</th>
                  <th>Expected</th>
                </tr>
              </thead>
              <tbody>
                {d.open_pos.map((o) => (
                  <tr key={o.po_id}>
                    <td>{o.po_id}</td>
                    <td className="num">{num(o.qty)}</td>
                    <td>{o.order_date}</td>
                    <td>{o.expected_date}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {rm.notes?.length > 0 && (
            <ul className="small">
              {rm.notes.map((n, i) => (
                <li key={i}>{n}</li>
              ))}
            </ul>
          )}
        </Card>

        <Card title={`Supplier ${sup.supplier_id} · ${sup.name}`}>
          <table className="table compact kv">
            <tbody>
              <tr>
                <td>On-time rate</td>
                <td className="num">{pct(Number(sup.on_time_rate))}</td>
              </tr>
              <tr>
                <td>Fill rate</td>
                <td className="num">{pct(Number(sup.fill_rate))}</td>
              </tr>
              <tr>
                <td>Mean / P80 delay</td>
                <td className="num">
                  {num(Number(sup.mean_delay_days), 1)} / {num(Number(sup.p80_delay_days), 1)} days
                </td>
              </tr>
              <tr>
                <td>Lead time stated → effective</td>
                <td className="num">
                  {sup.stated_lead_time_days} → {sup.effective_lead_time_days} days
                </td>
              </tr>
              <tr>
                <td>Expedite</td>
                <td className="num">
                  {sup.expedite_lead_time_days} days at +{pct(Number(sup.expedite_premium_pct))}
                </td>
              </tr>
              <tr>
                <td>Receipts observed</td>
                <td className="num">{sup.n_receipts}</td>
              </tr>
              <tr>
                <td>Terms</td>
                <td className="num">{sup.terms}</td>
              </tr>
            </tbody>
          </table>

          <h3>Supplier-delay cases used by the planner</h3>
          <table className="table compact">
            <tbody>
              {d.supply_cases.map((c) => (
                <tr key={c.label}>
                  <td>{c.label}</td>
                  <td className="num">{pct(c.weight)}</td>
                  <td className="num">new PO arrives day {c.new_po_arrival_day ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3>Reorder math</h3>
          <p className="mono small">{rm.formula}</p>
          <table className="table compact kv">
            <tbody>
              <tr>
                <td>Mean daily demand d (forecast p50)</td>
                <td className="num">{num(Number(rm.mean_daily_demand), 2)}</td>
              </tr>
              <tr>
                <td>σ daily demand</td>
                <td className="num">{num(Number(rm.sigma_daily_demand), 2)}</td>
              </tr>
              <tr>
                <td>Planning lead time LT / σ LT</td>
                <td className="num">
                  {rm.lead_time_days} / {num(Number(rm.sigma_lead_time_days), 2)} days
                </td>
              </tr>
              <tr>
                <td>Service level z</td>
                <td className="num">{rm.z}</td>
              </tr>
              <tr>
                <td>Safety stock SS</td>
                <td className="num">{num(Number(rm.safety_stock), 1)}</td>
              </tr>
              <tr>
                <td>Reorder point ROP</td>
                <td className="num strong">{num(Number(rm.reorder_point), 1)}</td>
              </tr>
              <tr>
                <td>Inventory position</td>
                <td className={`num strong ${Number(rm.position) < Number(rm.reorder_point) ? 'bad-text' : ''}`}>
                  {num(Number(rm.position), 1)}
                </td>
              </tr>
            </tbody>
          </table>
        </Card>
      </div>

      <Card title="Alerts">
        <AlertList alerts={d.alerts} />
      </Card>
    </div>
  );
}

export default function SkuPage({ skuId }: { skuId?: string }) {
  return skuId ? <Detail skuId={skuId} /> : <SkuList />;
}
