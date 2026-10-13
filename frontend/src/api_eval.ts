import type { Job } from './api';

export type ExtractionMetrics = {
  n_messages: number;
  n_gold_units: number;
  n_predicted_units: number;
  precision: number | null;
  recall: number | null;
  f1: number | null;
  magnitude_accuracy: number | null;
  n_magnitude_checks: number;
  start_date_accuracy: number | null;
  n_date_checks: number;
  evidence_valid_rate: number | null;
  n_raw_events: number;
  invalid_rate: number | null;
  invalid_reasons: Record<string, number>;
  false_positive_rate_no_event: number | null;
  n_no_event_messages: number;
  message_accuracy: number | null;
  fallbacks: number;
  by_tag: Record<string, { n: number; correct: number }>;
};

export type UnitRow = {
  event_type: string;
  sku_id: string;
  expected_magnitude?: number | null;
  magnitude_range?: number[] | null;
  predicted_magnitude?: number | null;
  magnitude_ok?: boolean;
  start_ok?: boolean;
  expected_start?: string;
  predicted_start?: string | null;
};

export type MessageResult = {
  id: string;
  tags: string[];
  expect_none: boolean;
  method: string;
  n_gold: number;
  n_pred: number;
  tp: number;
  fp: number;
  fn: number;
  correct: boolean;
  matched: UnitRow[];
  misses: UnitRow[];
  false_positives: UnitRow[];
  invalid: { event_type: string; sku_ids: string[] | null; reason: string }[];
  text: string;
};

export type ValueOfSignals = {
  messages: string[];
  budget: number;
  n_signals: Record<string, number>;
  actual_world_loss: Record<'none' | 'llm' | 'gold', number>;
  captured_actual: number | null;
  mc_mean_loss: Record<'none' | 'llm' | 'gold', number>;
  captured_mc: number | null;
  n_mc_worlds: number;
  extractor: string;
  note: string;
};

export type ExtractionEval = {
  gold_messages: number;
  as_of: string;
  model: string | null;
  llm: ExtractionMetrics | null;
  heuristic: ExtractionMetrics;
  per_message: MessageResult[];
  value_of_signals: ValueOfSignals | null;
  cache: { hits: number | null; misses: number | null };
  llm_seconds: number;
  computed_at: string;
  note: string;
};

export type EvalJob = Pick<Job, 'id' | 'kind' | 'status' | 'error' | 'created_at' | 'finished_at'>;

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, { ...init, headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) } });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      if (typeof j.detail === 'string') msg = j.detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(msg || `HTTP ${r.status}`);
  }
  return r.json() as Promise<T>;
}

export const evalApi = {
  extraction: () => call<{ result: ExtractionEval | null; job: EvalJob | null }>('/api/eval/extraction'),
  runExtraction: () => call<EvalJob>('/api/eval/extraction', { method: 'POST' }),
};
