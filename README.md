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
| Forecasts (hybrid) | A statistical base is always computed: Holt-Winters (damped trend, weekly seasonality) or Croston/SBA for intermittent SKUs. p10/p90 come from rolling in-sample errors. Known calendar/context events and confirmed signals are applied as multipliers. The LLM may only **retune those event multipliers within bounds** (plus at most two small extra adjustments); it never writes the series. Invalid output means a labelled statistical forecast. |
| Service level | SKUs in the top 20% by 28-day revenue are **hero** SKUs; each unit short costs a goodwill penalty (15% of price for heroes, 5% for others) in the optimiser. The operator can pin any SKU to hero / standard / auto. Plans report fill rate and hero fill rate. |
| Supply uncertainty | Each supplier's historical delays give early / typical / late cases (P15/P50/P85, weighted 30/40/30), with the same delay applied to every PO from that supplier. Confirmed delay signals are a floor. |
| Markdowns | Concave price-elasticity uplift (diminishing returns at deep discounts), shelf-life aware, and **cannibalisation**: 30% of the extra units are assumed to come from the category's other SKUs at their average margin. |
| Planner | A stochastic simulator scores candidate actions (standard or expedited PO, partial buys, markdowns, combined, defer) per SKU. An exact multiple-choice knapsack then picks the best set under the cash budget. |
| Budget frontier | The same candidate menu is solved at 0, ¼, ½, ¾, 1, 1.5 and 2 × the budget, showing expected loss and fill rate per budget level. Any point can be re-planned in one click. |
| Baselines | A **static** reorder-point rule (stated lead time, 28-day average) and a **tuned** rule whose safety and cover days are grid-searched on the planning world. The plan has to beat both. |
| Robustness | A background job scores the plan, both baselines and (when signals exist) the plan without signals over 120 Monte Carlo demand/supply worlds. It reports p5/p50/p95 savings and win rates. It also simulates **weekly re-planning** (days 0/7/14/21) against plan-once, using rolling lots, POs, budget and markdowns. |
| Overrides and plan diff | The operator can change quantities or discounts, or skip any line, on a draft plan. Edited plans are re-scored with the same simulator and stay within budget; the robustness job re-runs. Each new plan lists what changed vs the previous one and why. |
| Explanation | The LLM writes a plan narrative that may only use numbers, dates and IDs present in the plan JSON. It gets one corrective retry; after that, a deterministic template is used. |
| Scenarios | A free-text "what if" (for example "SUP-04 delays 7 more days" or "budget is cut to 30k") is parsed into a validated spec. The response shows the current plan next to an adapted plan. |
| Backtest | Rolling-origin forecast backtest: three origins, a 14-day horizon and the top 10 SKUs. It reports pooled WAPE and p10–p90 coverage for seasonal naive, naive + events, statistical and hybrid. |
| Approve & export | Approve a plan, then download PO, markdown and action CSVs. Analytics tables can also be exported. |

Every LLM call is logged (`/api/llm/calls`) with a status of `ok`, `ok_repaired`, `invalid`, `error` or `unavailable`. The UI shows a badge for whether each output came from a **validated model** or a **labelled fallback**.

## Results on the synthetic dataset (₹50,000 budget)

All values are simulated, not measured business results. They come from a fresh seed (`scripts.seed --no-generate`, all 30 forecasts hybrid) and a plan generated in the UI.

| Measure | Plan | Tuned rule | Static rule |
|---|---|---|---|
| Expected 30-day loss, evaluation world | ₹117.4k | ₹126.8k | ₹140.3k |
| Mean loss over 120 Monte Carlo worlds | ₹124.7k | ₹134.4k | ₹146.4k |
| Savings vs this baseline (mean, p5–p95) | – | ₹9.7k (₹4.0k–₹17.1k), wins in 100% of worlds | ₹21.6k (₹13.7k–₹29.1k), wins in 100% of worlds |
| Fill rate / hero fill rate | 0.910 / 0.816 | 0.899 / 0.817 | 0.897 / 0.817 |

Budget frontier (evaluation world):

| Budget | ₹0 | ₹12.5k | ₹25k | ₹37.5k | ₹50k | ₹75k | ₹100k |
|---|---|---|---|---|---|---|---|
| Loss | ₹177.5k | ₹162.2k | ₹144.0k | ₹129.9k | ₹117.4k | ₹96.7k | ₹76.9k |
| Fill rate | 0.835 | 0.847 | 0.867 | 0.899 | 0.910 | 0.937 | 0.954 |

Forecast backtest (origins 1, 15 and 29 Sep; 14-day horizon; top 10 SKUs):

| Model | WAPE | p10–p90 coverage |
|---|---|---|
| Seasonal naive | 26.4% | 66% |
| Naive + events | 25.6% | 67% |
| Statistical | 24.2% | 80% |
| Hybrid (latest origin only, 10/10 valid) | 23.5% | 81% |

Honest caveats:
- **The LLM step adds nothing measurable here.** On the same SKUs and origin, the statistical forecast scores 23.4% and the hybrid 23.5%. In the synthetic data the context-feed priors equal the true event effects, so there is nothing for the model to correct. Showing its value needs real data.
- **Weekly re-planning gains only about ₹130 over plan-once** at ₹50k, because the optimiser spends the whole budget on day 0. Re-planning weekly still beats a weekly-run static rule by about ₹22k.
- **The plan does not improve hero fill rate** (0.816 vs 0.817): its savings come from total loss and overall fill. Raising the hero goodwill penalty or pinning SKUs as hero shifts that balance.
- The rolling simulation feeds back realised demand, not censored sales.

## Architecture

```
frontend/  React + TypeScript + Vite + Recharts (hash routes: #/plan #/inbox #/sku #/scenario)
backend/   FastAPI + SQLAlchemy 2 + PostgreSQL 16
  app/api/routes.py        HTTP API (also serves the built frontend when STATIC_DIR exists)
  app/llm/                 Gemini on Vertex AI / OpenAI-compatible clients, prompts, JSON-task runner with validation
  app/services/            signals, forecasting, simulator, planner, value, rolling re-plan, scenarios, plans/jobs
  app/adapters/            storage (local folder or Cloud Storage), search, analytics
  scripts/seed.py          synthetic data generator and loader
data/seed/                 generated CSVs and evaluation truth (committed so the demo is reproducible)
data/messages/             three demo messages
Dockerfile                 multi-stage: builds the frontend, then one FastAPI image for Cloud Run
cloudbuild.yaml            CI: build, push to Artifact Registry, roll out to Cloud Run
infra/gcp/deploy.sh        one-shot Google Cloud setup and deploy
infra/docker-compose.yml   local PostgreSQL 16 (port 5433)
```

Each external service sits behind an adapter selected by configuration:

| Concern | Local development | Google Cloud |
|---|---|---|
| LLM | Any OpenAI-compatible endpoint | Gemini (`gemini-2.5-flash`) on Vertex AI via `google-genai`, service-account auth, native JSON output |
| Operational DB | PostgreSQL 16 in Docker | Cloud SQL for PostgreSQL 16 (Unix socket; URL kept in Secret Manager) |
| Files (messages, uploads, exports, forecast cache) | `var/` folder | Cloud Storage bucket |
| Hosting | uvicorn + Vite | One Cloud Run service (API + UI on the same origin), plus a Cloud Run job for seeding |
| Product/supplier search | PostgreSQL full-text/trigram | Same, on Cloud SQL |
| Analytics | PostgreSQL + CSV export | CSV exports in Cloud Storage (BigQuery-ready) |

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
| `LLM_PROVIDER` | `auto` | `vertex`, `openai_compatible`, or `auto` (Vertex when a Google Cloud project is set). |
| `GOOGLE_CLOUD_PROJECT` | – | Also `GCP_PROJECT`. Enables Gemini on Vertex AI with Application Default Credentials (`gcloud auth application-default login` locally). |
| `GCP_LOCATION` | `global` | Vertex AI location. |
| `VERTEX_MODEL` | `gemini-2.5-flash` | Gemini model id. |
| `LITELLM_BASE_URL` | – | OpenAI-compatible endpoint for local development. Also accepts `LLM_BASE_URL`, `OPENAI_BASE_URL` and `BASE_URL`. |
| `LITELLM_API_KEY` | – | Also accepts `LLM_API_KEY`, `OPENAI_API_KEY` and `API_KEY`. |
| `LLM_MODEL` | `gpt-5.4-mini` | Model for the OpenAI-compatible endpoint. |
| `LLM_DISABLED` | `false` | Set to `true` to force the labelled fallbacks everywhere. |
| `DATABASE_URL` | local Docker DB | SQLAlchemy URL. Cloud SQL: `postgresql+psycopg://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE`. |
| `STORAGE_BACKEND` | `local` | `gcs` stores files in `GCS_BUCKET` (optional `GCS_PREFIX`). |
| `STATIC_DIR` | `frontend/dist` | Built UI served by FastAPI when present. |
| `EVAL_WORLDS` / `ROLLING_WORLDS` | `120` / `20` | Monte Carlo worlds for the robustness job. |

Without LLM credentials the app still works end to end, using statistical and template fallbacks that are clearly labelled.

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

## Deploy on Google Cloud

From the repo root, in Cloud Shell (or any shell with `gcloud` and `openssl`):

```bash
PROJECT_ID=my-project ./infra/gcp/deploy.sh            # IAM-protected service
PROJECT_ID=my-project PUBLIC=1 ./infra/gcp/deploy.sh   # public demo URL
```

The script is idempotent. It does the following:
1. Enables the required APIs.
2. Creates an Artifact Registry repo, a runtime service account (Vertex AI user, Cloud SQL client) and a private Cloud Storage bucket.
3. Creates a Cloud SQL PostgreSQL 16 instance, database and user. The generated `DATABASE_URL` goes into Secret Manager.
4. Builds the image with Cloud Build.
5. Deploys the Cloud Run service (region `asia-south1` by default) with `LLM_PROVIDER=vertex` and `STORAGE_BACKEND=gcs`.
6. Creates and runs the `inventory-rescue-seed` Cloud Run job to load the demo data.

The service uses `--no-cpu-throttling` and `--min-instances 1`, because the robustness evaluation and forecast refresh run after the HTTP response returns. Later releases can use `gcloud builds submit --config cloudbuild.yaml` (or a trigger on `main`), which builds, pushes and rolls out the new image.

Local container check: `docker build -t inventory-rescue . && docker run -p 8080:8080 -e DATABASE_URL=... inventory-rescue`.

## 5-minute demo

1. **Plan**: open `#/plan` and click **Generate plan** (₹50,000 budget). Review:
   - savings vs the tuned and static rules, fill rate and hero fill rate;
   - the action table with reasons;
   - the budget frontier.
   
   The robustness card fills in after about 20 seconds with Monte Carlo savings ranges, win rates and the weekly re-plan comparison.
2. **Inbox**: open `#/inbox`. Click each demo-message button (supplier delay for SUP-04, an influencer spike on SKU-019, an expiring cold-brew lot), then click **Extract signals**. Check each event's quoted evidence, then **Confirm** the delay and the spike.
3. **Re-plan**: back on `#/plan`, click **Re-plan**. The "what changed" card lists the lines that moved and why: extra SKU-019 for the spike, and a workaround for the delayed POs. With signals confirmed, the robustness card also shows the plan-without-signals ablation.
4. **Override**: click **Edit plan**, change a quantity or discount or skip a line, and apply. The plan is re-scored and stays within budget.
5. **Approve**: click **Approve & export CSVs** and download the PO and markdown files.
6. **SKU drill-down**: on `#/sku/SKU-019`, see the forecast band, the supplier-delay cases and the hero/standard priority toggle.
7. **Scenarios**: on `#/scenario`, try "SUP-04 delays another 7 days" or "budget is cut to 30k", then click **Run backtest**.

## LLM safeguards

- The LLM only proposes. Every output is parsed as JSON and validated by code: schemas, known IDs, verbatim evidence, numeric bounds, forecast sanity checks and event reflection. Invalid output is never used silently.
- Extracted signals affect plans only after a human confirms them.
- Optimisation, simulation and money calculations are deterministic code. The LLM never chooses quantities.
- Explanations may only quote values that appear in the computed plan.

## Known limitations

- The data is synthetic and covers a single warehouse. Value figures are simulated estimates. See the caveats under Results.
- The presentation deck predates the robustness work, so its numbers are older than those in this README.
- On Windows with WSL Docker, WSL can idle out and stop PostgreSQL. Keep a WSL session open, for example `wsl -e bash -c "exec sleep infinity"`, while you demo.
- Background jobs run in-process. On Cloud Run this needs CPU always allocated, which `deploy.sh` sets. At larger scale, move them to Cloud Tasks or Cloud Run jobs.
- There is no user authentication in the app. Use the IAM-protected Cloud Run deployment (the default) for anything beyond a demo.
