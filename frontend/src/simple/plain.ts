// Plain-language helpers for the Simple view: no jargon, no codes, Indian number formatting.
import type { PlanAction, Signal } from '../api';

export const rupees = (v: number | null | undefined) =>
  v === null || v === undefined || Number.isNaN(v) ? '—' : '₹' + Math.round(v).toLocaleString('en-IN');

export const count = (v: number) => Math.round(v).toLocaleString('en-IN');

/** '2026-11-02' -> '2 Nov' (parsed as a local date so it never shifts a day). */
export function day(iso: string | null | undefined): string {
  if (!iso) return '';
  const m = iso.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return iso;
  const d = new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  return d.toLocaleDateString('en-IN', { day: 'numeric', month: 'short' });
}

export function listNames(names: string[]): string {
  if (names.length <= 1) return names[0] ?? 'some products';
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`;
}

/** One plain sentence describing what a message said. */
export function signalSentence(s: Signal, nameOf: (id: string) => string): string {
  const who = listNames(s.sku_ids.map(nameOf));
  const when =
    s.start_date && s.end_date
      ? ` from ${day(s.start_date)} to ${day(s.end_date)}`
      : s.start_date
        ? ` from ${day(s.start_date)}`
        : '';
  const m = s.magnitude;
  switch (s.event_type) {
    case 'supplier_delay':
      return `${who} will arrive about ${Math.round(m)} days late.`;
    case 'demand_spike':
      return `More people will buy ${who}${when}: about ${Number(m.toFixed(1))} times the usual.`;
    case 'demand_drop':
      return `Fewer people will buy ${who}${when}: about ${Math.round((1 - m) * 100)}% less than usual.`;
    case 'expiry_risk':
      return m ? `${count(m)} units of ${who} may go out of date soon.` : `Some ${who} may go out of date soon.`;
    default:
      return `Something may change for ${who}.`;
  }
}

/** What to do, as short plain lines. */
export function actionSteps(a: PlanAction): string[] {
  const out: string[] = [];
  if (a.order_qty > 0) {
    const arrive = a.arrival_date ? `, arrives ${day(a.arrival_date)}` : '';
    out.push(
      a.expedite
        ? `Buy ${count(a.order_qty)} more with fast delivery${arrive}.`
        : `Buy ${count(a.order_qty)} more${arrive}.`,
    );
  }
  if (a.later_qty) {
    out.push(`Then buy ${count(a.later_qty)} more on ${day(a.later_order_date)} if still needed.`);
  }
  if (a.discount_pct) {
    out.push(`Put the old stock on sale at ${Math.round(a.discount_pct * 100)}% off so it sells before it goes bad.`);
  }
  return out;
}

/** The single most useful reason, rewritten in plain words. */
export function actionWhy(a: PlanAction): string {
  const stockout = () => {
    for (const r of a.reasons) {
      const so = r.match(/stockout expected from (\d{4}-\d{2}-\d{2})/);
      if (so) return `Otherwise it runs out around ${day(so[1])}.`;
    }
    return '';
  };
  const waste = () => {
    for (const r of a.reasons) {
      const ex = r.match(/: (\d+) units expected to expire unsold/);
      if (ex) return `Otherwise ${count(Number(ex[1]))} units would go to waste.`;
    }
    return '';
  };
  // A sale is about waste; a purchase is about running out.
  return a.order_qty > 0 ? stockout() || waste() : waste() || stockout();
}

/** Safety net for the plain-language prompt: drop record ids the model left in, use ₹. */
export function tidyAnswer(t: string): string {
  return t
    .replace(/\s*\((?:SKU|SUP|LOT|PO|EV)-[\w-]+\)/g, '')
    .replace(/\s+(?:on|from|of|in)\s+lot\s+LOT-[\w-]*\w/gi, '')
    .replace(/\bINR\s?/g, '₹')
    .replace(/(\d)\.0 lakh/g, '$1 lakh');
}

export const kindIcon = (a: PlanAction) => (a.order_qty > 0 ? (a.expedite ? 'truck' : 'cart') : 'tag') as 'truck' | 'cart' | 'tag';
