// Thin typed client for the FastAPI backend (proxied by Vite in dev).

export type Alert = {
  id: number; entity_id: string; alert_type: string; severity: string; message: string;
  details: Record<string, unknown> | null; first_seen_at: string; updated_at: string;
};

export type Job = {
  id: number; kind: string; status: 'queued' | 'running' | 'done' | 'failed'; payload: Record<string, unknown>;
  result: Record<string, unknown> | null; error: string | null; created_at: string; finished_at: string | null;
};

export type Overview = {
  as_of: string; default_budget: number; n_skus: number;
  forecasts: { fresh: number; total: number; llm: number; fallback: number; latest: string | null };
  pending_signals: number; alerts: Alert[]; active_jobs: Job[]; llm_configured: boolean;
};

export type Health = { status: string; db: string; llm_configured: boolean; llm_model: string | null };

export type PlanAction = {
  id: string; rank: number; sku_id: string; sku_name: string; category: string; supplier_id: string;
  kind: string; label: string; order_qty: number; expedite: boolean; arrival_date: string | null;
  markdown_lot_id: string | null; discount_pct: number | null; cash_outlay: number; expedite_premium: number;
  expected_loss_if_deferred: number; expected_loss_with_action: number; loss_avoided: number;
  forecast_method: string; reasons: string[]; record_ids: string[];
};

export type Totals = {
  lost_margin: number; expiry_writeoff: number; markdown_cost: number; expedite_premium: number;
  cash_outlay: number; units_short: number; expired_units: number; markdown_units: number;
  ending_units: number; ending_value: number; total_loss: number; stockout_sku_days: number;
};

export type Sensitivity = { case: string; plan_total_loss: number; baseline_total_loss: number; savings: number };

export type Plan = {
  id: number; as_of: string; budget: number; status: string; created_at: string; approved_at: string | null;
  actions: PlanAction[];
  baseline_actions: { sku_id: string; kind: string; order_qty: number; cash_outlay: number }[];
  summary: {
    budget: number; cash_used: number; cash_remaining: number; expected_loss_no_action: number;
    expected_loss_plan: number; n_actions: number; skus_at_risk: number;
    forecast_methods: Record<string, number>;
    scorecard: { world: string; plan: Totals; baseline: Totals; savings: number };
    scorecard_per_sku: Record<string, { plan: Totals; baseline: Totals }>;
    sensitivity: Sensitivity[];
    evaluation_world: Record<string, unknown>; planning_world: Record<string, unknown>;
  };
  explanation: { method: string; error: string | null; summary: string; bullets: { text: string; cites: string[] }[] };
  exports: { po_csv?: string; markdown_csv?: string; po_lines?: number; markdown_lines?: number } | null;
};

export type Signal = {
  id: number; ref: string; message_id: number; event_type: string; sku_ids: string[]; magnitude: number;
  start_date: string | null; end_date: string | null; quoted_evidence: string; status: string;
  rejection_reason: string | null; created_at: string;
};

export type IngestResult = { message_id: number; extraction_method: string; llm_error: string | null; events: Signal[] };

export type SkuRow = {
  sku_id: string; name: string; category: string; supplier_id: string; on_hand: number; open_po_qty: number;
  avg_daily_28d: number; days_of_cover: number | null; forecast_method: string; forecast_fresh: boolean;
};

export type SkuDetail = {
  sku_id: string; name: string; category: string; unit_cost: number; unit_price: number; case_pack: number;
  moq: number; shelf_life_days: number; as_of: string;
  history: { date: string; units: number }[];
  forecast: {
    id: number | null; method: string; fallback_reason: string | null; fresh: boolean; created_at: string | null;
    drivers: string[]; days: { date: string; p10: number; p50: number; p90: number }[];
  };
  lots: { lot_id: string; qty: number; received_date: string; expiry_date: string; days_to_expiry: number }[];
  open_pos: { po_id: string; qty: number; order_date: string; expected_date: string }[];
  supplier: Record<string, string | number> & { supplier_id: string; name: string };
  reorder: Record<string, number | string | string[]> & { notes: string[]; formula: string };
  alerts: Alert[];
};

export type ScenarioResult = {
  parse_method: string; parse_error: string | null; spec: Record<string, unknown>; budget: number;
  current_budget: number; current_cash_used: number;
  outcomes_in_scenario: { current_plan: Totals; adapted_plan: Totals; baseline: Totals };
  adapted_cash_used: number;
  changed_actions: { sku_id: string; sku_name: string; before: string; after: string }[];
};

export type Backtest = {
  holdout_start: string; holdout_days: number; skus: number; llm_valid_forecasts: number;
  wape_naive_all: number; wape_llm_on_valid: number | null; wape_naive_on_valid: number | null;
  per_sku: { sku_id: string; llm_method: string; wape_llm: number | null; wape_naive: number }[];
  computed_at: string; note: string;
};

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const isForm = init?.body instanceof FormData;
  const r = await fetch(path, {
    ...init,
    headers: isForm ? init?.headers : { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      const d = j.detail;
      if (typeof d === 'string') msg = d;
      else if (d && typeof d.message === 'string')
        msg =
          d.message +
          (Array.isArray(d.errors)
            ? ': ' + d.errors.map((x: { line: number; error: string }) => `line ${x.line}: ${x.error}`).join('; ')
            : '');
      else if (Array.isArray(d)) msg = d.map((x: { msg?: string }) => x.msg ?? JSON.stringify(x)).join('; ');
      else msg = JSON.stringify(d ?? j);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(r.status, msg);
  }
  return r.json() as Promise<T>;
}

const post = <T,>(path: string, body?: unknown) =>
  req<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) });

export const api = {
  health: () => req<Health>('/health'),
  overview: () => req<Overview>('/api/overview'),
  skus: () => req<SkuRow[]>('/api/skus'),
  sku: (id: string) => req<SkuDetail>(`/api/skus/${encodeURIComponent(id)}`),
  demoMessages: () => req<{ name: string; text: string }[]>('/api/messages/demo'),
  postMessage: (text: string, source = 'paste') => post<IngestResult>('/api/messages', { text, source }),
  uploadMessage: (f: File) => {
    const fd = new FormData();
    fd.append('file', f);
    return req<IngestResult>('/api/messages/upload', { method: 'POST', body: fd });
  },
  importSales: (f: File) => {
    const fd = new FormData();
    fd.append('file', f);
    return req<{ imported: number; skus: string[]; job: Job }>('/api/import/sales', { method: 'POST', body: fd });
  },
  signals: () => req<Signal[]>('/api/signals'),
  confirm: (id: number) => post<{ signal: Signal; job: Job | null }>(`/api/signals/${id}/confirm`),
  reject: (id: number, reason?: string) =>
    post<{ signal: Signal; job: Job | null }>(`/api/signals/${id}/reject`, { reason: reason ?? null }),
  jobs: () => req<Job[]>('/api/jobs'),
  job: (id: number) => req<Job>(`/api/jobs/${id}`),
  refreshForecasts: (sku_ids: string[] | null, force = false) => post<Job>('/api/forecasts/refresh', { sku_ids, force }),
  latestPlan: async (): Promise<Plan | null> => {
    try {
      return await req<Plan>('/api/plans/latest');
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) return null;
      throw e;
    }
  },
  createPlan: (budget: number) => post<Plan>('/api/plans', { budget }),
  approvePlan: (id: number) => post<Plan>(`/api/plans/${id}/approve`),
  scenario: (question: string, budget: number | null) => post<ScenarioResult>('/api/scenarios', { question, budget }),
  backtest: () => req<Backtest | null>('/api/backtest'),
  runBacktest: () => post<Job>('/api/backtest'),
};

export const inr = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits });

export const num = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? '—' : v.toLocaleString('en-IN', { maximumFractionDigits: digits });

export const pct = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined ? '—' : `${(v * 100).toFixed(digits)}%`;

export async function waitForJob(id: number, onUpdate?: (j: Job) => void, timeoutMs = 180_000): Promise<Job> {
  const t0 = Date.now();
  for (;;) {
    const j = await api.job(id);
    onUpdate?.(j);
    if (j.status === 'done' || j.status === 'failed') return j;
    if (Date.now() - t0 > timeoutMs) return j;
    await new Promise((r) => setTimeout(r, 1200));
  }
}
