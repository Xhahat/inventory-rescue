# Inventory Rescue

Inventory Rescue turns unstructured supplier and campaign messages, together with sales history, into a **budget-constrained recovery plan** for a single-warehouse food and nutrition brand. For each SKU it recommends one action: buy, expedite, mark down or defer. It also shows the **simulated value** of the plan against a rule-based reorder-point baseline.

The AI Builder Cup presentation is in
[`docs/Inventory_Rescue_AI_Builder_Cup.pptx`](docs/Inventory_Rescue_AI_Builder_Cup.pptx).

> All values are simulated on a seeded, synthetic dataset with known "truth" demand. They are not
> measured business results.

## What it does

| Area | Details |
|---|---|
| Signal inbox | Paste or upload a supplier email or campaign note. The LLM extracts typed events (`supplier_delay`, `demand_spike`, `demand_drop`, `expiry_risk`), each with a verbatim quote as evidence. Every event is validated and must be confirmed by a human before it affects a plan. |
| Forecasts | 30-day p10/p50/p90 forecasts per SKU from the LLM, validated against a seasonal-naive benchmark and confirmed events. A labelled statistical fallback is used when validation fails. |
| Planner | A stochastic simulator scores candidate actions (standard or expedited PO, markdown, combined, defer) under a cash budget. A multiple-choice knapsack then picks the best set. |
| Value scorecard | Compares the plan's expected loss (stockouts, waste, markdown, holding and expedite costs) with no action and with a reorder-point baseline. Scoring uses evaluation "truth" demand. |
| Explanation | The LLM writes a plan narrative that may only use numbers, dates and IDs present in the plan JSON. It gets one corrective retry; after that, a deterministic template is used. |
| Scenarios | A free-text "what if" (for example "SUP-04 delays 7 more days" or "budget is cut to 30k") is parsed into a validated spec. The response shows the current plan next to an adapted plan. |
| Backtest | Plan vs baseline over several historical as-of dates. |
| Approve & export | Approve a plan, then download PO, markdown and action CSVs. Analytics tables can also be exported. |

Every LLM call is logged (`/api/llm/calls`) with a status of `ok`, `ok_repaired`, `invalid`, `error` or `unavailable`. The UI shows a badge for whether each output came from a **validated LLM** or a **labelled fallback**.

## Architecture

```
frontend/  React + TypeScript + Vite + Recharts (hash routes: #/plan #/inbox #/sku #/scenario)
backend/   FastAPI + SQLAlchemy 2 + PostgreSQL 16
  app/api/routes.py        HTTP API
  app/llm/                 LiteLLM client (openai SDK), prompts, JSON-task runner with validation and repair
  app/services/            signals, forecasting, simulator, planner, value, scenarios, explanation, plans/jobs
  app/adapters/            storage / search / analytics adapters (local now, Google Cloud later)
  scripts/seed.py          synthetic data generator and loader
data/seed/                 generated CSVs and evaluation truth (committed so the demo is reproducible)
data/messages/             three demo messages
infra/docker-compose.yml   PostgreSQL 16 (port 5433)
```

Each external service sits behind an adapter:

| Concern | Local (this repo) | Google Cloud target |
|---|---|---|
| LLM | LiteLLM via the `openai` SDK | Gemini on Vertex AI |
| Operational DB | PostgreSQL 16 in Docker | Cloud SQL for PostgreSQL |
| Files | `var/` folder | Cloud Storage |
| Product/supplier search | PostgreSQL full-text/trigram | Vertex AI Search |
| Analytics | PostgreSQL + CSV export | BigQuery / Looker |
| Hosting | uvicorn + Vite | Cloud Run + Secret Manager |

## Setup

Prerequisites: Python 3.12, Node 20+ and Docker. On Windows, Docker can run inside WSL.

```powershell
# 1. Database (PostgreSQL on localhost:5433)
cd infra
docker compose up -d          # Windows + WSL Docker: wsl -e docker compose up -d

# 2. Backend
cd ..
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 3. Frontend
cd frontend
npm install
```

### Configuration

Settings are read from environment variables, or from `.env` / `env/.env` at the repo root. Both are git-ignored.

| Variable | Default | Notes |
|---|---|---|
| `LITELLM_BASE_URL` | – | Also accepts `LLM_BASE_URL`, `OPENAI_BASE_URL` and `BASE_URL`. |
| `LITELLM_API_KEY` | – | Also accepts `LLM_API_KEY`, `OPENAI_API_KEY` and `API_KEY`. |
| `LLM_MODEL` | `gpt-5.4-mini` | Any model your LiteLLM proxy allows. |
| `LLM_DISABLED` | `false` | Set to `true` to force the labelled fallbacks everywhere. |
| `DATABASE_URL` | local Docker DB | SQLAlchemy URL (`postgresql+psycopg://...`). |

Without LLM credentials the app still works end to end, using heuristic and template fallbacks that are clearly labelled.

## Run

```powershell
cd backend
..\.venv\Scripts\python.exe -m scripts.seed        # generate + load data, compute forecasts (~30 s with LLM)
..\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

cd ..\frontend
npm run dev                                        # http://localhost:5173 (proxies /api to :8000)
```

Seed options:
- `--no-generate` reloads the existing CSVs.
- `--no-forecast` skips forecasting.
- `--force-forecast` ignores the forecast cache in `var/cache`.

## Tests

```powershell
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q          # uses the DB; LLM calls are faked
$env:IRC_LIVE_LLM='1'; ..\.venv\Scripts\python.exe -m pytest tests\test_live_llm.py -q   # optional live check
```

Coverage includes:
- the LLM output validators: schema, verbatim evidence, unknown SKUs or IDs, invented numbers, and forecasts that ignore confirmed events;
- fallbacks and the repair retry;
- planner budget feasibility and value against the baseline;
- the scenario parser and spec bounds;
- the full API flow.

## 5-minute demo

1. **Plan**: open `#/plan` and click **Generate plan** (₹50,000 budget). Review the KPI scorecard (expected loss vs no action and vs the baseline), the action table with reasons and record IDs, and the explanation badge.
2. **Inbox**: open `#/inbox`. Click each demo-message button (supplier delay for SUP-04, an influencer spike on SKU-019, an expiring cold-brew lot), then click **Extract signals**. Check each event's quoted evidence, then **Confirm** the delay and the spike.
3. **Re-plan**: back on `#/plan`, click **Re-plan**. The plan now buys extra SKU-019 for the spike and works around the delayed POs. Savings go from about ₹29k to about ₹32k at ₹50k. Click **Approve & export CSVs** and download the PO and markdown files.
4. **SKU drill-down**: on `#/sku/SKU-019`, the sales history and forecast band show the confirmed spike.
5. **Scenarios**: on `#/scenario`, try "SUP-04 delays another 7 days", "SKU-019 demand doubles for 2 weeks" or "budget is cut to 30k". Compare the current and adapted plans, then click **Run backtest**.

## LLM safeguards

- The LLM only proposes. Every output is parsed as JSON and validated by code: schemas, known IDs, verbatim evidence, numeric bounds, forecast sanity checks and event reflection. Invalid output is never used silently.
- Extracted signals affect plans only after a human confirms them.
- Optimisation, simulation and money calculations are deterministic code. The LLM never chooses quantities.
- Explanations may only quote values that appear in the computed plan.

## Known limitations

- The data is synthetic and covers a single warehouse. Value figures are simulated estimates.
- On Windows with WSL Docker, WSL can idle out and stop PostgreSQL. Keep a WSL session open, for example `wsl -e bash -c "exec sleep infinity"`, while you demo.
- Jobs run in-process on threads. A production deployment would use Cloud Tasks or Cloud Run jobs instead.
