import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { ActivityFeed } from '../api';
import { Badge, Card, ErrorBox, Spinner } from '../ui';

const KIND: Record<string, [string, string]> = {
  message: ['neutral', 'Message read'],
  signal_confirmed: ['ok', 'Signal confirmed'],
  signal_rejected: ['warn', 'Signal rejected'],
  plan_created: ['neutral', 'Plan built'],
  plan_approved: ['ok', 'Plan approved'],
  question: ['neutral', 'Question'],
  import: ['neutral', 'Sales import'],
};

function when(ts: string): string {
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

export default function ActivityPage() {
  const [feed, setFeed] = useState<ActivityFeed | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .activity(50)
      .then((f) => {
        setFeed(f);
        setErr(f.error ?? null);
      })
      .catch((e) => setErr(String(e.message ?? e)));
  }, []);

  useEffect(() => {
    load();
    const t = window.setInterval(load, 15_000);
    return () => window.clearInterval(t);
  }, [load]);

  return (
    <Card
      title="Activity journal"
      actions={
        <>
          {feed && (
            <Badge kind={feed.backend === 'firestore' ? 'ok' : 'neutral'}>
              {feed.backend === 'firestore' ? 'Stored in Firestore' : `Stored: ${feed.backend}`}
            </Badge>
          )}{' '}
          <button className="small" onClick={load}>
            Refresh
          </button>
        </>
      }
    >
      <p className="muted small">
        Who read which message, which signals were confirmed or rejected, which plans were built and approved, and what
        was asked. Shared by every server instance, newest first.
      </p>
      <ErrorBox error={err} />
      {!feed && !err && <Spinner label="Loading…" />}
      {feed && !feed.items.length && !err && <p className="muted">Nothing recorded yet.</p>}
      {feed && feed.items.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>When</th>
              <th>What</th>
              <th>Details</th>
            </tr>
          </thead>
          <tbody>
            {feed.items.map((i, n) => {
              const [k, label] = KIND[i.kind] ?? ['neutral', i.kind];
              return (
                <tr key={`${i.ts}-${n}`}>
                  <td className="small" style={{ whiteSpace: 'nowrap' }}>{when(i.ts)}</td>
                  <td>
                    <Badge kind={k}>{label}</Badge>
                    <div>{i.title}</div>
                  </td>
                  <td className="muted small">{i.detail}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </Card>
  );
}
