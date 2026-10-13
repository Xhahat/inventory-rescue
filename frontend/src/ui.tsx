import type { ReactNode } from 'react';
import type { Alert } from './api';

export function Badge({ kind, children, title }: { kind?: string; children: ReactNode; title?: string }) {
  return (
    <span className={`badge badge-${kind ?? 'neutral'}`} title={title}>
      {children}
    </span>
  );
}

export function MethodBadge({ method, reason }: { method: string; reason?: string | null }) {
  const map: Record<string, [string, string]> = {
    llm: ['ok', 'LLM (validated)'],
    fallback: ['warn', 'Fallback: seasonal-naive'],
    heuristic: ['warn', 'Fallback: rule-based parser'],
    template: ['warn', 'Fallback: template'],
  };
  const [k, label] = map[method] ?? ['neutral', method];
  return (
    <Badge kind={k} title={reason ?? undefined}>
      {label}
    </Badge>
  );
}

export function Kpi({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className={`kpi ${tone ? 'kpi-' + tone : ''}`}>
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Card({ title, children, actions }: { title?: ReactNode; children: ReactNode; actions?: ReactNode }) {
  return (
    <section className="card">
      {(title || actions) && (
        <header className="card-head">
          <h2>{title}</h2>
          <div className="card-actions">{actions}</div>
        </header>
      )}
      {children}
    </section>
  );
}

export function ErrorBox({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="error">{error}</div>;
}

export function Spinner({ label }: { label?: string }) {
  return (
    <span className="spinner-wrap">
      <span className="spinner" /> {label}
    </span>
  );
}

const SEV: Record<string, string> = { high: 'bad', medium: 'warn', low: 'neutral' };
const ALERT_LABEL: Record<string, string> = {
  stockout_before_arrival: 'Stockout before arrival',
  expiry: 'Expiry risk',
  overstock: 'Overstock',
  supplier_reliability: 'Supplier reliability',
};

export function AlertList({ alerts, compact }: { alerts: Alert[]; compact?: boolean }) {
  if (!alerts.length) return <p className="muted">No active alerts.</p>;
  return (
    <ul className="alerts">
      {alerts.map((a) => (
        <li key={a.id}>
          <Badge kind={SEV[a.severity] ?? 'neutral'}>{a.severity}</Badge>{' '}
          <strong>{ALERT_LABEL[a.alert_type] ?? a.alert_type}</strong>{' '}
          {a.entity_id.startsWith('SKU-') ? <a href={`#/sku/${a.entity_id}`}>{a.entity_id}</a> : a.entity_id}
          {!compact && <div className="muted small">{a.message}</div>}
        </li>
      ))}
    </ul>
  );
}

export function RecordChip({ id }: { id: string }) {
  const m = id.match(/^(?:ACT-)?(SKU-\d{3})$/) || id.match(/^LOT-(\d{3})-/);
  const sku = m ? (m[1].startsWith('SKU-') ? m[1] : `SKU-${m[1]}`) : null;
  return sku ? (
    <a className="chip" href={`#/sku/${sku}`}>
      {id}
    </a>
  ) : (
    <span className="chip">{id}</span>
  );
}
