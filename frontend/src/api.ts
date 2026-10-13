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
  forecasts: { fresh: number; total: number; hybrid: number; statistical: number; latest: string | null };
  pending_signals: number; alerts: Alert[]; active_jobs: Job[]; llm_configured: boolean;
};

export type Health = { status: string; db: string; llm_configured: boolean; llm_model: string | null };

export type PlanAction = {
  id: string; rank: number; sku_id: string; sku_name: string; category: string; supplier_id: string;
  kind: string; label: string; order_qty: number; expedite: boolean; arrival_date: string | null;
  later_qty?: number; later_order_date?: string | null; later_arrival_date?: string | null; cash_today?: number;
  markdown_lot_id: string | null; discount_pct: number | null; cash_outlay: number; expedite_premium: number;
  expected_loss_if_deferred: number; expected_loss_with_action: number; loss_avoided: number;
  forecast_method: string; reasons: string[]; record_ids: string[];
  raw: { order_qty: number; expedite: boolean; markdown: { lot_id: string; discount_pct: number } | null };
  overridden?: boolean; original?: string;
};

export type Totals = {
  lost_margin: number; expiry_writeoff: number; markdown_cost: number; expedite_premium: number;
  cash_outlay: number; units_short: number; expired_units: number; markdown_units: number;
  ending_units: number; ending_value: number; total_loss: number; stockout_sku_days: number;
  cannibalisation: number; demand_units: number; fill_rate: number | null;
};

export type Dist = { mean: number; p5: number; p50: number; p95: number };
export type FrontierRow = {
  budget: number; cash_used: number; planning_loss: number; planning_objective: number; eval_loss: number;
  eval_fill_rate: number | null; eval_hero_fill_rate: number | null; n_actions: number; is_current: boolean;
};
export type RollingRun = {
  total_loss: number; fill_rate: number | null; hero_fill_rate: number | null; cash_spent: number; orders_placed: number;
  cash_day0?: number; capital_cost?: number; loss_incl_service?: number; tranches_released?: number;
};
export type Strategy = 'commit_now' | 'staggered' | 'reserve' | 'lean';
export type PlanOption = {
  key: Strategy; label: string; description: string; is_current: boolean; operating_budget: number;
  cash_today: number; cash_scheduled: number; cash_committed: number; cash_uncommitted: number;
  planning_loss: number; planning_objective: number; capital_cost: number; eval_loss_no_review: number;
  eval_fill_rate: number | null; eval_hero_fill_rate: number | null; n_actions: number; n_staggered: number;
};
export type OptionWorldStats = {
  total_loss: Dist; loss_incl_service: Dist; capital_cost: number; fill_rate: number; hero_fill_rate: number;
  cash_spent: number; tranches_released: number;
};
export type OptionsEval = {
  n_calm: number; n_shock: number; notes: string;
  best: Partial<Record<'actual' | 'calm' | 'shock', Strategy>>;
  best_service: Partial<Record<'actual' | 'calm' | 'shock', Strategy>>;
  options: Record<string, {
    actual: RollingRun; calm?: OptionWorldStats; shock?: OptionWorldStats;
    vs_commit_now?: Partial<Record<'calm' | 'shock', Dist & { win_rate: number }>>;
  }>;
};
export type Robustness = {
  status: 'queued' | 'done' | 'failed'; job_id?: number; error?: string; computed_at?: string; notes?: string;
  monte_carlo?: {
    n_worlds: number;
    policies: Record<string, { total_loss: Dist; fill_rate: Dist; hero_fill_rate: Dist; units_short: Dist }>;
    savings: Record<string, Dist & { win_rate: number; loss_rate: number }>;
  };
  rolling?: {
    review_days: number[]; actual_world: Record<string, RollingRun>; n_mc_worlds: number;
    mc: Record<string, { total_loss: Dist; fill_rate: Dist }>;
    comparisons: Record<string, Dist & { win_rate: number }>; notes: string;
  };
  options?: OptionsEval | null;
  signal_ablation?: {
    status: string; note?: string; n_changed_skus?: number;
    actual_world?: { with_signals: number; without_signals: number; value_of_signals: number };
    monte_carlo?: Dist & { win_rate: number };
  };
};
export type PlanDiff = {
  previous_plan_id: number; previous_as_of: string; previous_budget: number; n_added: number; n_removed: number;
  n_changed: number; unchanged: number; delta_expected_loss: number; delta_cash: number;
  changes: { sku_id: string; sku_name: string; change: string; before: string; after: string; cash_delta: number }[];
};
export type OverrideItem = { sku_id: string; order_qty?: number; skip?: boolean; discount_pct?: number };

export type Sensitivity = { case: string; plan_total_loss: number; baseline_total_loss: number; savings: number };

export type Plan = {
  id: number; as_of: string; budget: number; status: string; created_at: string; approved_at: string | null;
  actions: PlanAction[];
  baseline_actions: { sku_id: string; kind: string; order_qty: number; cash_outlay: number }[];
  summary: {
    budget: number; cash_used: number; cash_remaining: number; expected_loss_no_action: number;
    strategy?: Strategy; strategy_label?: string; cash_today?: number; cash_scheduled?: number;
    options?: PlanOption[];
    expected_loss_plan: number; n_actions: number; skus_at_risk: number;
    forecast_methods: Record<string, number>;
    scorecard: {
      world: string; plan: Totals; baseline: Totals; savings: number; tuned_baseline: Totals; savings_vs_tuned: number;
    };
    scorecard_per_sku: Record<string, { plan: Totals; baseline: Totals; tuned_baseline: Totals }>;
    sensitivity: Sensitivity[];
    service: {
      hero_skus: string[]; goodwill_pct: { hero: number; standard: number };
      plan_fill_rate: number | null; baseline_fill_rate: number | null; tuned_fill_rate: number | null;
      plan_hero_fill_rate: number | null; baseline_hero_fill_rate: number | null; tuned_hero_fill_rate: number | null;
      planning_service_penalty: number;
    };
    tuned_baseline: {
      safety_days: number; cover_days: number; planning_world_loss: number; grid: string;
      actions: { sku_id: string; kind: string; order_qty: number; cash_outlay: number }[];
    };
    frontier: FrontierRow[];
    robustness?: Robustness;
    diff?: PlanDiff | null;
    revision?: number;
    overrides?: { at: string; items: OverrideItem[] }[];
    overridden_removed?: { sku_id: string; sku_name: string; original: string }[];
    evaluation_world: Record<string, unknown>; planning_world: Record<string, unknown>;
  };
  explanation: {
    method: string; error: string | null; summary: string; bullets: { text: string; cites: string[] }[]; stale_note?: string;
  };
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
  priority: string; priority_overridden: boolean;
};

export type Priority = { priority: string; auto: string; overridden: boolean; revenue_28d: number };

export type SkuDetail = {
  sku_id: string; name: string; category: string; unit_cost: number; unit_price: number; case_pack: number;
  moq: number; shelf_life_days: number; as_of: string;
  priority: Priority & { goodwill_per_unit: number };
  supply_cases: { label: string; weight: number; new_po_arrival_day: number | null }[];
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
  origins: string[]; holdout_start: string; holdout_days: number; skus: number; hybrid_valid_forecasts: number;
  pooled: Record<string, { wape: number | null; coverage_p10_p90: number | null; n_points: number }>;
  per_origin: Record<string, string | number | null>[];
  wape_statistical_on_hybrid_skus: number | null;
  hybrid_attempted?: number;
  statistical_on_hybrid_points?: { wape: number | null; coverage_p10_p90: number | null; n_points: number } | null;
  text_only_subset?: {
    n_series: number;
    statistical: { wape: number | null; coverage_p10_p90: number | null; n_points: number };
    hybrid: { wape: number | null; coverage_p10_p90: number | null; n_points: number };
  } | null;
  sku_selection?: { top_volume: string[]; context_event: string[]; text_only_event: string[] };
  per_sku: {
    sku_id: string; wape_naive: number | null; wape_statistical: number | null; hybrid_method: string | null;
    wape_hybrid: number | null; selected_by?: string; text_only_event?: boolean;
  }[];
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
  createPlan: (budget: number, strategy: Strategy = 'commit_now') => post<Plan>('/api/plans', { budget, strategy }),
  approvePlan: (id: number) => post<Plan>(`/api/plans/${id}/approve`),
  plan: (id: number) => req<Plan>(`/api/plans/${id}`),
  overridePlan: (id: number, items: OverrideItem[]) => post<Plan>(`/api/plans/${id}/overrides`, { items }),
  setPriority: (sku: string, priority: 'hero' | 'standard' | 'auto') =>
    req<Priority>(`/api/skus/${encodeURIComponent(sku)}/priority`, { method: 'PUT', body: JSON.stringify({ priority }) }),
  scenario: (question: string, budget: number | null) => post<ScenarioResult>('/api/scenarios', { question, budget }),
  backtest: () => req<Backtest | null>('/api/backtest'),
  runBacktest: () => post<Job>('/api/backtest'),
};

export const inr = (v: number | null | undefined, digits = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? '—'
    : '₹' + v.toLocaleString('en-IN', { maximumFractionDigits: digits, minimumFractionDigits: digits });

/** Compact INR for chart axes: ₹45k, ₹2.5L (lakh), ₹1.2Cr (crore). */
export const inrShort = (v: number) => {
  const a = Math.abs(v);
  const fmt = (x: number, unit: string) => `${v < 0 ? '-' : ''}₹${Number(x.toFixed(x < 10 ? 1 : 0))}${unit}`;
  if (a >= 1e7) return fmt(a / 1e7, 'Cr');
  if (a >= 1e5) return fmt(a / 1e5, 'L');
  if (a >= 1e3) return fmt(a / 1e3, 'k');
  return `${v < 0 ? '-' : ''}₹${Math.round(a)}`;
};

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
