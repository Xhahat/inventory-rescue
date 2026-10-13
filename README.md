# Inventory Rescue

Inventory Rescue turns unstructured supplier and campaign messages, together with sales history, into a **budget-constrained recovery plan** for a single-warehouse food and nutrition brand. For each SKU it recommends one action: buy, expedite, mark down or defer. It also shows the **simulated value** of the plan against a rule-based reorder-point baseline.

The AI Builder Cup presentation is in
[`docs/Inventory_Rescue_AI_Builder_Cup.pptx`](docs/Inventory_Rescue_AI_Builder_Cup.pptx).

> All values are simulated on a seeded, synthetic dataset with known "truth" demand. They are not
> measured business results.

## What it does

| Area | Details |
|---|---|
| Signal inbox | Paste or upload a supplier email or campaign note. The LLM extracts typed events (`supplier_delay`, `demand_spike`, `demand_drop`, `expiry_risk`), each with a verbatim quote as evidence. Every event is validated and must be confirmed by a human before it affects a plan. An **extraction-quality card** scores the same extraction path on a 40-message labelled set. |
| Forecasts (hybrid) | A statistical base is always computed: Holt-Winters (damped trend, weekly seasonality) or Croston/SBA for intermittent SKUs. p10/p90 come from rolling in-sample errors. Known calendar/context events and confirmed signals are applied as multipliers. About a third of the context feed is **text only** (a competitor recall, a heatwave, a homepage banner, an influencer reel): the statistical forecast treats those as ×1.0, and only the LLM can turn the description into a bounded multiplier. The LLM may only **retune event multipliers within bounds** (plus at most two small extra adjustments, each with a stated driver); it never writes the series. Invalid output means a labelled statistical forecast. |
| Service level | SKUs in the top 20% by 28-day revenue are **hero** SKUs; each unit short costs a goodwill penalty (15% of price for heroes, 5% for others) in the optimiser. The operator can pin any SKU to hero / standard / auto. Plans report fill rate and hero fill rate. |
| Supply uncertainty | Each supplier's historical delays give early / typical / late cases (P15/P50/P85, weighted 30/40/30), with the same delay applied to every PO from that supplier. Confirmed delay signals are a floor. |
| Markdowns | Concave price-elasticity uplift (diminishing returns at deep discounts), shelf-life aware, and **cannibalisation**: 30% of the extra units are assumed to come from the category's other SKUs at their average margin. |
| Planner | A stochastic simulator scores candidate actions (standard or expedited PO, partial buys, markdowns, combined, defer) per SKU. An exact multiple-choice knapsack then picks the best set under the cash budget. |
| Plan options | The same budget is planned four ways: **commit now** (spend on day 0), **staggered buys** (larger buys split into a now tranche and a day-7/14 tranche that goes back to that weekly review), **keep a reserve** (30% held for the reviews) and **lean** (smallest budget keeping 90% of the savings). Each shows cash paid today, scheduled and held back, plus paired results with weekly re-planning in calm and shocked futures. One click re-plans with any option; the PO export marks scheduled lines. |
| Budget frontier | The same candidate menu is solved at 0, ¼, ½, ¾, 1, 1.5 and 2 × the budget, showing expected loss and fill rate per budget level. Any point can be re-planned in one click. |
| Baselines | A **static** reorder-point rule (stated lead time, 28-day average) and a **tuned** rule whose safety and cover days are grid-searched on the planning world. The plan has to beat both. |
| Robustness | A background job scores the plan, both baselines and (when signals exist) the plan without signals over 120 Monte Carlo demand/supply worlds. It reports p5/p50/p95 savings and win rates. It also simulates **weekly re-planning** (days 0/7/14/21) against plan-once, using rolling lots, POs, budget and markdowns. |
| Overrides and plan diff | The operator can change quantities or discounts, or skip any line, on a draft plan. Edited plans are re-scored with the same simulator and stay within budget; the robustness job re-runs. Each new plan lists what changed vs the previous one and why. |
| Explanation | The LLM writes a plan narrative that may only use numbers, dates and IDs present in the plan JSON. It gets one corrective retry; after that, a deterministic template is used. |
| Planning assistant | `#/assistant`: ask questions in plain language ("Why are we not expediting SKU-015?", "What if SUP-04 slips a week?"). The model answers by calling ten read-only tools over the planner and simulator, and every number in its answer is checked against those tool results. |
| Scenarios | A free-text "what if" (for example "SUP-04 delays 7 more days" or "budget is cut to 30k") is parsed into a validated spec. The response shows the current plan next to an adapted plan. |
| Backtest | Rolling-origin forecast backtest: three origins, a 14-day horizon, the top 10 SKUs plus every SKU with a context event in a hold-out window (25 SKUs). It reports pooled WAPE and p10–p90 coverage for seasonal naive, naive + events, statistical and hybrid, overall and on the text-only series. |
| Approve & export | Approve a plan, then download PO, markdown and action CSVs. Analytics tables can also be exported. |

Every LLM call is logged (`/api/llm/calls`) with a status of `ok`, `ok_repaired`, `invalid`, `error` or `unavailable`. The UI shows a badge for whether each output came from a **validated model** or a **labelled fallback**.

## Results on the synthetic dataset (₹50,000 budget)

All values are simulated, not measured business results. They come from a fresh seed (`scripts.seed`, all 30 forecasts hybrid, live model `gpt-5.4-mini`) and a plan generated in the UI.

| Measure | Plan | Tuned rule | Static rule |
|---|---|---|---|
| Expected 30-day loss, evaluation world | ₹126.5k | ₹135.2k | ₹147.9k |
| Mean loss over 120 Monte Carlo worlds | ₹135.0k | ₹144.1k | ₹154.5k |
| Savings vs this baseline (mean, p5–p95) | – | ₹9.2k (₹4.4k–₹16.7k), wins in 100% of worlds | ₹19.6k (₹13.1k–₹26.1k), wins in 100% of worlds |
| Fill rate / hero fill rate | 0.913 / 0.766 | 0.896 / 0.792 | 0.897 / 0.792 |

Budget frontier (evaluation world):

| Budget | ₹0 | ₹12.5k | ₹25k | ₹37.5k | ₹50k | ₹75k | ₹100k |
|---|---|---|---|---|---|---|---|
| Loss | ₹186.5k | ₹170.8k | ₹153.5k | ₹141.0k | ₹126.5k | ₹103.1k | ₹83.2k |
| Fill rate | 0.831 | 0.840 | 0.888 | 0.889 | 0.913 | 0.937 | 0.962 |

### Plan options: when to spend the budget

Spending everything on day 0 leaves nothing for the weekly reviews: with commit-now, weekly re-planning gains ₹0 over plan-once. The plan-options card compares four uses of the same ₹50k. Each option is followed by weekly re-planning (days 0/7/14/21). "Calm" futures are 10 Monte Carlo worlds; "shock" futures add mid-month demand shifts (×0.5–1.8) on about 30% of SKUs and a 4–8 day slip at one supplier. Comparisons are paired: every option sees the same futures.

| Option | Paid on day 0 | Loss, actual world | Calm: vs commit now (win rate) | Shocks: vs commit now (win rate) | Hero fill, actual |
|---|---|---|---|---|---|
| Commit now | ₹50.0k | ₹126.5k | – | – | 0.766 |
| Staggered buys (4 split) | ₹27.1k | ₹122.9k | ₹1.3k better (80%) | ₹0.4k better (70%) | 0.792 |
| Keep a reserve (30%) | ₹35.0k | ₹122.2k | ₹4.1k better (100%) | ₹3.2k better (90%) | 0.766 |
| Lean (₹45k) | ₹44.9k | ₹132.3k | ₹5.8k worse (0%) | ₹5.1k worse (0%) | 0.766 |

Caveats:
- **Most of the reserve's gain comes from one forecast miss.** The hero SKU-019 is under-forecast, and the day-7 review buys more once its sales show up. The calm futures are centred on the actual month, so they share that miss; the shock futures are the fairer test, and there the reserve loses in 1 of 10.
- **Staggered buys halve day-0 cash at the same planning loss** and get the best hero fill rate, but their gain is small and uneven (p5 is ₹5–11k worse). Working-capital cost (18% a year) is reported separately and is not in the loss.
- Without weekly reviews, holding cash back is never better: the day-0 loss with no later buys is ₹141.7k for the reserve against ₹126.5k for commit now.

### Measuring the GenAI layer

Forecast backtest (3 origins × 14 days, 25 SKUs, 1,050 points):

| Model | WAPE | p10–p90 coverage |
|---|---|---|
| Seasonal naive | 32.0% | 69% |
| Naive + numeric events | 31.5% | 70% |
| Statistical | 29.9% | 77% |
| Hybrid (LLM-tuned events) | **29.1%** | 78% |
| Statistical, 16 text-only series | 31.5% | 73% |
| Hybrid, 16 text-only series | **28.8%** | 76% |

Inbox extraction, 40 labelled messages (supplier emails, WhatsApp notes, Hindi/Hinglish, forwarded threads, retractions, vague maybes, unknown products, 14 no-event messages):

| Extractor | Precision | Recall | F1 | False event on no-event messages | Messages fully correct |
|---|---|---|---|---|---|
| LLM + validators | 0.97 | 1.00 | **0.98** | 1 in 14 | 97.5% |
| Keyword baseline | 0.94 | 0.50 | 0.65 | 0 | 57.5% |

Every evidence quote appears verbatim in its message. On the 5 demo messages tied to real events, plans built from the LLM-extracted signals recover 100% of the loss reduction that perfect information gives (₹126.5k → ₹118.4k in the evaluation world; ₹135.2k → ₹128.1k over 40 worlds).

Caveats:
- **The forecasting gain is modest:** 0.8 pp pooled, 2.7 pp on text-only series. The hybrid is better at every origin but worse on some SKUs (for example SKU-003, 41.2% → 47.0%). The LLM consistently underestimates text-only effects (×1.08–1.18 against true +25–60%) and got the heatwave's effect on tea wrong.
- **The extraction gold set is small and written by the developers**, and the prompt rules were refined after a first run on the same set (F1 0.94 before, 0.98 after). Treat it as a regression check, not a benchmark.
- **The plan does not improve hero fill rate** (0.766 against 0.792 for the rules); its savings come from total loss and overall fill. Raising the hero goodwill penalty or pinning SKUs as hero shifts that balance.
- The rolling simulation feeds back realised demand, not censored sales.

### Planning assistant

On seven live questions (gpt-5.4-mini), including a follow-up, every answer passed the number check on the first attempt in 3–8 s. Each used 1–4 tool calls: explaining a SKU decision, a 40k hero-first replan, a supplier slip, expiry markdowns, 50k vs 75k, and a supplier email draft.

### Model comparison

The same three evaluations ran on every chat model available on our OpenAI-compatible gateway. Calls that failed under load (timeouts, transient gateway errors) were retried, so the table reflects quality, not network luck. The statistical baseline is 29.8% WAPE overall and 31.5% on the text-only series. Assistant latency is the median per question, measured one model at a time; it includes tool execution (about 1 s).

| Model | Extraction F1 | Messages fully correct | False event on no-event msgs | Forecast WAPE, all / text-only | Assistant: validated (first try) | Assistant latency |
|---|---|---|---|---|---|---|
| gpt-5.4-mini (default) | 0.984 | 97.5% | 1 / 14 | 29.1% / 28.7% | 6/6 (6) | **4.0 s** |
| claudesonnet4.5 | **1.000** | **100%** | 0 | 29.1% / 28.8% | 6/6 (6) | 10.0 s |
| gpt-5.1 | **1.000** | **100%** | 0 | 29.4% / 29.8% | 6/6 (6) | 7.6 s |
| gpt-5.2 | 0.983 | 97.5% | 0 | 29.4% / 29.9% | 6/6 (6) | 9.2 s |
| gpt-5 | 0.983 | 97.5% | 0 | **28.9% / 27.9%** | 6/6 (6) | 31.1 s |
| gpt-5.3-chat | 0.983 | 97.5% | 0 | **28.9% / 27.9%** | 0/6: gateway rejects tool calls | – |
| gpt-5.6-luna | 0.983 | 97.5% | 0 | 29.5% / 30.6% | 6/6 (6) | 14.1 s |
| claudeopus5 | 0.983 | 97.5% | 0 | 29.4% / 29.8% | 6/6 (4) | 11.7 s |
| claudesonnet4.6 | 0.983 | 97.5% | 0 | 30.6% / 32.9% (worse than statistical) | 6/6 (5) | 7.9 s |
| qwen3.6-27b | 0.966 | 95.0% | 0 | 29.4% / 30.4% | 6/6 (3) | 18.1 s |

What this shows:
- **Extraction is close to saturated on this set.** Every model gets 38–40 of 40 messages right, and the differences are one or two messages. Seven models miss g26 ("cartons close to best-before, count pending"). gpt-5.2, claudeopus5 and claudesonnet4.6 leave the unit count empty, and the validator rejects an expiry event without a number, so this is partly a validator choice. gpt-5, gpt-5.3-chat, gpt-5.6-luna and qwen return no event. qwen also misquotes the Hindi message's evidence. gpt-5.4-mini is the only model that invents an event for a product not in the catalogue.
- **Forecasting is where models differ.** On text-only series, gpt-5 and gpt-5.3-chat cut WAPE by about 3.5 pp against the statistical model, and gpt-5.4-mini and claudesonnet4.5 by about 2.7 pp. Most others manage 1–2 pp; gpt-5.6-luna under 1 pp. claudesonnet4.6 is worse than the statistical model. Every hybrid passed validation (75/75 forecasts).
- **Speed:** gpt-5.4-mini is about 2–8× faster than the rest. gpt-5 is the most accurate forecaster but about 8× slower; its uncached batch backtest took about 13 minutes under load against about 1.5 for gpt-5.4-mini.
- **Gateway quirks:** gpt-5, gpt-5.6-luna and claudeopus5 reject `temperature=0`. The client now drops a rejected parameter and retries; before that, these models fell back on every JSON task. gpt-5.3-chat is routed to a deployment that refuses tool calls on chat completions, so the assistant falls back to its labelled tool summary.
- **Gemini, the target on Google Cloud, has not been measured yet.** It isn't on this gateway, and the Vertex path is only covered by unit tests. Run the same evaluations on Vertex before the demo.
- With 40 messages, 75 forecasts and 6 questions, gaps of one or two messages, or under about 0.5 pp WAPE, are within noise. The extraction prompt rules were refined on gpt-5.4-mini's first-run errors.

## Architecture

```
frontend/  React + TypeScript + Vite + Recharts (hash routes: #/plan #/inbox #/sku #/scenario #/assistant)
backend/   FastAPI + SQLAlchemy 2 + PostgreSQL 16
  app/api/routes.py        HTTP API (also serves the built frontend when STATIC_DIR exists)
  app/api/assistant_routes.py, eval_routes.py   planning assistant and extraction-evaluation endpoints
  app/llm/                 Gemini on Vertex AI / OpenAI-compatible clients, prompts, JSON-task runner with validation, tool-calling agent
  app/services/            signals, forecasting, simulator, planner, value, rolling re-plan and plan options, scenarios, plans/jobs, assistant tools, extraction eval
  app/adapters/            storage (local folder or Cloud Storage), search, analytics
  scripts/seed.py          synthetic data generator and loader
data/seed/                 generated CSVs and evaluation truth (committed so the demo is reproducible)
data/messages/             five demo messages
data/eval/                 labelled inbox messages for the extraction evaluation
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
| `OPTION_WORLDS` | `10` | Calm worlds for the plan-options comparison (the same number of shock worlds is added). |
| `WORKING_CAPITAL_RATE` / `RESERVE_SHARE` / `LEAN_SAVINGS_SHARE` | `0.18` / `0.3` / `0.9` | Plan-option settings: annual cost of cash, share held back by "keep a reserve", share of savings "lean" must keep. |

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
- staggered buys, the four plan options, shock worlds and the paired option comparison;
- the planning assistant's tools, number checker and agent loop (with a faked model);
- extraction-evaluation matching and scoring;
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
   
   The robustness card fills in after about 45 seconds with Monte Carlo savings ranges, win rates, the weekly re-plan comparison and the plan-options results.
2. **Plan options**: in the "Plan options" card, compare cash paid today against loss in calm and shocked futures, then click **Use this option** on *Staggered buys* to see split buys ("48 now … · +60 ordered 2026-10-27").
3. **Inbox**: open `#/inbox`. Click a demo-message button (supplier delay for SUP-04, an influencer spike on SKU-019, an expiring cold-brew lot, a suppressed listing, a customs hold), then click **Extract signals**. Check each event's quoted evidence, then **Confirm** the delay and the spike. Scroll down to the **Extraction quality** card for precision/recall on the labelled set.
4. **Re-plan**: back on `#/plan`, click **Re-plan**. The "what changed" card lists the lines that moved and why: extra SKU-019 for the spike, and a workaround for the delayed POs. With signals confirmed, the robustness card also shows the plan-without-signals ablation.
5. **Override**: click **Edit plan**, change a quantity or discount or skip a line, and apply. The plan is re-scored and stays within budget.
6. **Approve**: click **Approve & export CSVs** and download the PO and markdown files.
7. **Assistant**: on `#/assistant`, ask "Why are we not expediting SKU-015?" or "What happens if SUP-04 slips another week?" and open the tool trace under the answer.
8. **SKU drill-down**: on `#/sku/SKU-019`, see the forecast band, the supplier-delay cases and the hero/standard priority toggle.
9. **Scenarios**: on `#/scenario`, try "SUP-04 delays another 7 days" or "budget is cut to 30k", then click **Run backtest**.

## LLM safeguards

- The LLM only proposes. Every output is parsed as JSON and validated by code: schemas, known IDs, verbatim evidence, numeric bounds, forecast sanity checks and event reflection. Invalid output is never used silently.
- Extracted signals affect plans only after a human confirms them.
- Optimisation, simulation and money calculations are deterministic code. The LLM never chooses quantities.
- Explanations may only quote values that appear in the computed plan.
- The planning assistant can only call read-only tools with validated arguments (known SKU and supplier ids, bounded numbers). Previews and scenarios run in memory, and emails are drafts that are never sent. Every number, date and id in an answer must appear in that conversation's tool results or the question; after one failed correction the reply falls back to a labelled summary of the tool results.

## Known limitations

- The data is synthetic and covers a single warehouse. Value figures are simulated estimates. See the caveats under Results.
- The presentation deck predates the robustness work, so its numbers are older than those in this README.
- On Windows with WSL Docker, WSL can idle out and stop PostgreSQL. Keep a WSL session open, for example `wsl -e bash -c "exec sleep infinity"`, while you demo.
- Background jobs run in-process. On Cloud Run this needs CPU always allocated, which `deploy.sh` sets. At larger scale, move them to Cloud Tasks or Cloud Run jobs.
- There is no user authentication in the app. Use the IAM-protected Cloud Run deployment (the default) for anything beyond a demo.
