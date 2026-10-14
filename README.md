# Inventory Rescue

Inventory Rescue turns unstructured supplier and campaign messages, together with sales history, into a **budget-constrained recovery plan** for a single-warehouse food and nutrition brand. For each SKU it recommends one action: buy, expedite, mark down or defer. It also shows the **simulated value** of the plan against a rule-based reorder-point baseline.

The AI Builder Cup presentation is in
[`docs/Inventory_Rescue_AI_Builder_Cup.pptx`](docs/Inventory_Rescue_AI_Builder_Cup.pptx).

> All values are simulated on a seeded, synthetic dataset with known "truth" demand. They are not
> measured business results.

**Who it is for.** Mid-size and large D2C / FMCG brands, where a month's stock-outs, write-offs and rush orders
run into lakhs of rupees. The demo brand sells 30 SKUs through D2C, Amazon, quick-commerce and retail: about
5,500 units a day, ₹4.3 crore of revenue a month, ₹2.5 crore of stock at cost, and a ₹10 lakh monthly rescue budget.
At that size the simulated savings (₹1.6–4.0 lakh a month, see Results) are 11–38× the cost of running the tool
on Google Cloud (about ₹10.5–14k a month for a dedicated deployment). For a small brand with a ₹50k budget the
same deployment would cost about as much as it saves, so small brands are better served by a shared, multi-tenant
deployment.

## What it does

| Area | Details |
|---|---|
| Simple view (default) | The landing page is built for someone new to computers. It asks one question, "What would you like to do?", and offers three large buttons: **Make my plan**, **Read a message** and **Ask a question**. With the budget, the user picks how to buy: all now, in two steps, keeping money aside, or spending less. Everything is in plain words: product names instead of codes, "runs out around 2 Nov", "put the old stock on sale at 20% off", and yes/no buttons to confirm what a message said. Assistant answers use a plain-language style (`style: "simple"`) that keeps the same number checks. Each screen has a **← Back** button. The full planner tools below sit behind the **Advanced view** link. |
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
| Scenarios | A free-text "what if" (for example "SUP-04 delays 7 more days" or "budget is cut to 6 lakh") is parsed into a validated spec. The response shows the current plan next to an adapted plan. |
| Backtest | Rolling-origin forecast backtest: three origins, a 14-day horizon, the top 10 SKUs plus every SKU with a context event in a hold-out window (25 SKUs). It reports pooled WAPE and p10–p90 coverage for seasonal naive, naive + events, statistical and hybrid, overall and on the text-only series. |
| Approve & export | Approve a plan, then download PO, markdown and action CSVs. Analytics tables can also be exported. |

Every LLM call is logged (`/api/llm/calls`) with a status of `ok`, `ok_repaired`, `invalid`, `error` or `unavailable`. The UI shows a badge for whether each output came from a **validated model** or a **labelled fallback**.

## Results on the synthetic dataset (₹10 lakh budget)

All values are simulated, not measured business results. They come from a fresh seed (`scripts.seed`, all 30 forecasts hybrid, live model `gemini-3.5-flash-lite` on the Gemini API, SQLite database) and a ₹10 lakh plan from `POST /api/plans`. "L" is lakh (₹1,00,000).

| Measure | Plan | Tuned rule | Static rule |
|---|---|---|---|
| Expected 30-day loss, evaluation world | ₹26.59L | ₹28.15L | ₹30.59L |
| Mean loss over 120 Monte Carlo worlds | ₹28.13L | ₹29.98L | ₹31.91L |
| Savings vs this baseline (mean, p5–p95) | – | ₹1.84L (₹1.08L–₹2.95L), wins in 100% of worlds | ₹3.78L (₹2.32L–₹5.35L), wins in 100% of worlds |
| Fill rate / hero fill rate | 0.897 / 0.803 | 0.888 / 0.752 | 0.886 / 0.752 |

The loss is large next to the budget on purpose: ₹10 lakh cannot fix every SKU at risk, so the planner has to choose. Plan generation takes about 6 s and the robustness job under a minute on a laptop. (With `gpt-5.4-mini` forecasts the same plan saved ₹1.94L / ₹3.88L; the gap is within the forecast noise.)

Budget frontier (evaluation world):

| Budget | ₹0 | ₹2.5L | ₹5L | ₹7.5L | ₹10L | ₹15L | ₹20L |
|---|---|---|---|---|---|---|---|
| Loss | ₹38.88L | ₹35.45L | ₹32.06L | ₹29.18L | ₹26.59L | ₹21.60L | ₹17.96L |
| Fill rate | 0.819 | 0.826 | 0.839 | 0.878 | 0.897 | 0.929 | 0.943 |

### Plan options: when to spend the budget

Spending everything on day 0 leaves nothing for the weekly reviews: with commit-now, weekly re-planning gains ₹0 over plan-once. The plan-options card compares four uses of the same ₹10 lakh. Each option is followed by weekly re-planning (days 0/7/14/21). "Calm" futures are 10 Monte Carlo worlds; "shock" futures add mid-month demand shifts (×0.5–1.8) on about 30% of SKUs and a 4–8 day slip at one supplier. Comparisons are paired: every option sees the same futures.

| Option | Paid on day 0 | Loss, actual world | Calm: vs commit now (win rate) | Shocks: vs commit now (win rate) | Hero fill, actual |
|---|---|---|---|---|---|
| Commit now | ₹9.99L | ₹26.59L | – | – | 0.803 |
| Staggered buys (3 split) | ₹6.47L | ₹26.76L | ₹0.33L worse (50%) | ₹0.49L worse (40%) | 0.823 |
| Keep a reserve (30%) | ₹7.00L | ₹25.38L | ₹1.08L better (100%) | ₹0.91L better (90%) | 0.854 |
| Lean (₹9L) | ₹8.99L | ₹27.37L | ₹0.91L worse (0%) | ₹0.78L worse (0%) | 0.798 |

Caveats:
- **Keeping a reserve is the best option here**, and it also gives the best hero fill rate (0.854). Part of its gain comes from the hero SKU-019 being under-forecast: the day-7 review buys more once its sales show up. The calm futures are centred on the actual month, so they share that miss; the shock futures are the fairer test, and there the reserve still wins 9 of 10 (p5 is ₹0.78L worse).
- **Staggered buys cut day-0 cash by a third (₹6.47L vs ₹9.99L) at the same planning loss** and raise hero fill, but at this scale they lose slightly on average (about 1–1.5% of the loss), because the later tranches are re-planned and partly redirected. They are a cash-flow choice, not a loss reducer. Working-capital cost (18% a year) is reported separately and is not in the loss: ₹12.4k vs ₹14.8k for commit now.
- Without weekly reviews, holding cash back is never better: the day-0 loss with no later buys is ₹29.51L for the reserve against ₹26.59L for commit now.

### Measuring the GenAI layer

Forecast backtest (3 origins × 14 days, 25 SKUs, 1,050 points, `gemini-3.5-flash-lite`):

| Model | WAPE | p10–p90 coverage |
|---|---|---|
| Seasonal naive | 24.1% | 70% |
| Naive + numeric events | 23.6% | 70% |
| Statistical | 23.0% | 75% |
| Hybrid (LLM-tuned events) | **21.5%** | 76% |
| Statistical, 16 text-only series | 23.9% | 71% |
| Hybrid, 16 text-only series | **22.2%** | 76% |

All 75 hybrid forecasts passed validation.

Inbox extraction, 40 labelled messages (supplier emails, WhatsApp notes, Hindi/Hinglish, forwarded threads, retractions, vague maybes, unknown products, 14 no-event messages):

| Extractor | Precision | Recall | F1 | False event on no-event messages | Messages fully correct |
|---|---|---|---|---|---|
| LLM + validators | 1.00 | 0.93 | **0.97** | 0 in 14 | 95.0% |
| Keyword baseline | 0.94 | 0.50 | 0.65 | 0 | 57.5% |

Every accepted evidence quote appears verbatim in its message. The validator rejected one event whose quote was paraphrased (the second PO in a two-PO delay email), which is the recall miss besides the count-less expiry note g26.

Value of the signals in a plan: on the 5 demo messages tied to real events, a ₹10 lakh plan built from the LLM-extracted signals cuts the evaluation-world loss from ₹26.59L (no signals) to ₹25.64L, the same as the plan built from hand-labelled (gold) signals: **100% of the perfect-information gain** (₹27.75L → ₹27.50L over 40 worlds, also 100%). Gemini extracted 4 of the 5 gold signals (it missed the second PO in g01); the missing delay does not change the plan.

This metric caught a real bug. The first run on the enterprise data captured **−17%**: every model read "from Oct 15 … for about 10 days" as ending on 25 October, while the planner treats end dates as inclusive (24 October). One extra day of 3× demand on the hero SKU-019 made the budget-tight plan put ₹9.98L into that SKU and starve two others. The extraction prompt now states that dates are inclusive (with an unrelated example), which fixed it for all 10 models. It also shows that a small misread can move a lakh, so signals still need human confirmation.

Caveats:
- **The forecasting gain is real but moderate:** 1.5 pp pooled and 1.7 pp on text-only series, measured on 1,050 points of one synthetic brand. Gains of this size need confirming on real sales history.
- **The extraction gold set is small and written by the developers**, and the prompt was refined after runs on the same set (rules for unknown products, retractions and corrections; then the inclusive-date rule above). Treat it as a regression check, not a benchmark. The date rule cost gpt-5.4-mini one message: it now also invents an event for an unlisted Mango Protein Bar (F1 0.984 → 0.968). Gemini invents no events but drops one paraphrased quote.
- **Plans are sensitive to the exact signal values** when the budget is tight (see the date bug above). A human should check dates and magnitudes before confirming; editing a signal in place is not built yet.
- The rolling simulation feeds back realised demand, not censored sales.

### Planning assistant

On six scripted questions (gemini-3.5-flash-lite), every answer passed the number check, five on the first attempt and one after a single repair, in 3–10 s (median 4.0 s). They cover explaining a SKU decision, an 8 lakh hero-first replan, a supplier slip, expiry markdowns, 10 lakh vs 15 lakh, and a supplier email draft.

### Model comparison

The same three evaluations ran on `gemini-3.5-flash-lite` through the Gemini API (AI Studio) and on every chat model available on our OpenAI-compatible gateway, on the enterprise dataset (₹10 lakh budget). Calls that failed under load (timeouts, transient gateway errors, free-tier rate limits) were retried, so the table reflects quality, not network luck. Forecast WAPE is shown against the statistical model on the same points; the statistical model alone is 23.0% overall and 23.9% on the 16 text-only series (24.2% on gpt-5.4-mini's 15, because one of its text-only forecasts failed validation). Assistant latency is the median per question, measured one model at a time; it includes tool execution.

| Model | Extraction F1 | Messages fully correct | Signal value kept | Forecast WAPE, all / text-only | Hybrid forecasts valid | Assistant: checked (first try) | Assistant latency |
|---|---|---|---|---|---|---|---|
| gemini-3.5-flash-lite (default) | 0.966 | 95.0% | 100% | 21.5% / 22.2% | 75/75 | 6/6 (5) | 4.0 s |
| gpt-5.4-mini | 0.968 | 95.0% | 100% | 21.5% / 21.9% | 74/75 | 6/6 (5) | **3.9 s** |
| claudesonnet4.5 | **1.000** | **100%** | 100% | **20.9%** / **19.0%** | 75/75 | 5/6 (5) | 10.3 s |
| gpt-5.1 | 0.983 | 97.5% | 100% | **20.9%** / 19.6% | 75/75 | 6/6 (6) | 11.9 s |
| gpt-5 | 0.983 | 97.5% | 100% | 21.0% / 19.2% | 73/75 | 6/6 (6) | 30.4 s |
| qwen3.6-27b | 0.966 | 95.0% | 100% | 21.0% / 20.2% | 74/75 | 6/6 (5) | 8.8 s |
| gpt-5.6-luna | 0.983 | 97.5% | 100% | 21.2% / 20.3% | 75/75 | 6/6 (6) | 14.8 s |
| claudeopus5 | 0.983 | 97.5% | 100% | 21.3% / 20.2% | 75/75 | 6/6 (4) | 12.3 s |
| gpt-5.2 | 0.983 | 97.5% | 100% | 21.3% / 21.2% | 75/75 | 6/6 (6) | 8.4 s |
| gpt-5.3-chat | 0.983 | 97.5% | 100% | 21.5% / 21.6% | 75/75 | 0/6: gateway rejects tool calls | – |
| claudesonnet4.6 | 0.983 | 97.5% | 100% | 21.6% / 20.1% | 75/75 | 6/6 (5) | 10.3 s |

What this shows:
- **Every model beats the statistical forecast**, by 1.4–2.2 pp overall and 1.7–4.9 pp on text-only series. claudesonnet4.5, gpt-5 and gpt-5.1 are the best forecasters; gemini-3.5-flash-lite (1.7 pp) and gpt-5.4-mini (2.3 pp) are the weakest on text-only series.
- **Extraction is close to saturated on this set.** Every model gets 38–40 of 40 messages right. Most miss g26 ("cartons close to best-before, count pending"), which gives no unit count; the validator rejects an expiry event without a number, so this is partly a validator choice. gpt-5.4-mini is the only model that invents events for products not in the catalogue (2 of 14 no-event messages). qwen also gets the Hindi Diwali-offer message wrong. After the date fix, every model's signals give the same plans as the gold signals.
- **The assistant works on every model with tool calling.** One claudesonnet4.5 answer fell back to the labelled tool summary. One Gemini answer began with a stray control character; the assistant now strips those before checking.
- **Speed:** gemini-3.5-flash-lite and gpt-5.4-mini are 2–8× faster than the rest. Flash-lite is the default because it is fast, is within one message of the larger models on extraction, passes every assistant check, and runs on Google's own API. A split is possible: a fast model for the assistant and extraction, a stronger one for the overnight forecast batch.
- **Gateway quirks:** gpt-5, gpt-5.6-luna and claudeopus5 reject `temperature=0`; the client drops a rejected parameter and retries. gpt-5.3-chat is routed to a deployment that refuses tool calls on chat completions, so the assistant falls back to its labelled tool summary.
- **Larger Gemini models are not measured yet.** On the AI Studio free tier, gemini-3.8-flash allows 20 requests a day (one evaluation run needs about 130) and was overloaded (HTTP 503) when tried; flash-lite allows 15 a minute, so its run used `GEMINI_RPM=14`. With billing enabled, set `GEMINI_MODEL` and rerun.
- With 40 messages, 75 forecasts and 6 questions, gaps of one or two messages, or under about 0.5 pp WAPE, are within noise.

## Architecture

```
frontend/  React + TypeScript + Vite + Recharts (Simple view: #/ #/s/plan #/s/message #/s/ask; Advanced view: #/plan #/inbox #/sku #/scenario #/assistant)
backend/   FastAPI + SQLAlchemy 2 (SQLite locally, PostgreSQL 16 / Cloud SQL on Google Cloud)
  app/api/routes.py        HTTP API (also serves the built frontend when STATIC_DIR exists)
  app/api/assistant_routes.py, eval_routes.py   planning assistant and extraction-evaluation endpoints
  app/llm/                 Gemini clients (AI Studio key or Vertex AI) and an optional OpenAI-compatible client, prompts, JSON-task runner with validation, tool-calling agent
  app/services/            signals, forecasting, simulator, planner, value, rolling re-plan and plan options, scenarios, plans/jobs, assistant tools, extraction eval
  app/adapters/            storage (local folder or Cloud Storage), search, analytics
  scripts/seed.py          synthetic data generator and loader
data/seed/                 generated CSVs and evaluation truth (committed so the demo is reproducible)
data/messages/             five demo messages
data/eval/                 labelled inbox messages for the extraction evaluation
Dockerfile                 multi-stage: builds the frontend, then one FastAPI image for Cloud Run
cloudbuild.yaml            CI: build, push to Artifact Registry, roll out to Cloud Run
infra/gcp/deploy.sh        one-shot Google Cloud setup and deploy
```

Each external service sits behind an adapter selected by configuration:

| Concern | Local development | Google Cloud |
|---|---|---|
| LLM | Gemini API with a Google AI Studio key (`google-genai`, native JSON output, function calling) | Same key from Secret Manager, or Gemini on Vertex AI with the service account |
| Operational DB | SQLite file `var/inventory_rescue.db` (no server needed; any SQLAlchemy URL works) | Cloud SQL for PostgreSQL 16 (Unix socket; URL kept in Secret Manager) |
| Files (messages, uploads, exports, forecast cache) | `var/` folder | Cloud Storage bucket |
| Hosting | uvicorn + Vite | One Cloud Run service (API + UI on the same origin), plus a Cloud Run job for seeding |
| Product/supplier search | Substring match | PostgreSQL trigram similarity on Cloud SQL |
| Analytics | CSV export | CSV exports in Cloud Storage (BigQuery-ready) |

## Setup

Prerequisites: Python 3.12, Node 20+ and a Gemini API key from [Google AI Studio](https://aistudio.google.com/apikey). No database server or Docker is needed locally: the app creates a SQLite file under `var/`.

```powershell
# 1. Backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt

# 2. Frontend
cd frontend
npm install
```

### Configuration

Settings are read from environment variables, or from `.env` / `env/.env` at the repo root. Both are git-ignored.

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | – | Google AI Studio key (also `GOOGLE_API_KEY`). Enables Gemini through the Gemini API. |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini model id (also `VERTEX_MODEL`). `gemini-3.8-flash` needs a billing-enabled key: its free tier allows 20 requests a day. |
| `GEMINI_THINKING_LEVEL` | `low` | `low`, `medium` or `high`; empty uses the model default. |
| `GEMINI_RPM` | `0` | Client-side cap on requests per minute (0 = none). Use `14` on the AI Studio free tier (15 RPM). 429 and 5xx responses are retried with backoff either way. |
| `LLM_PROVIDER` | `auto` | `gemini`, `vertex`, `openai_compatible`, or `auto`: the Gemini API when a key is set, else Vertex AI when a project is set, else the OpenAI-compatible endpoint. |
| `GOOGLE_CLOUD_PROJECT` | – | Also `GCP_PROJECT`. Used by the Vertex AI provider (Application Default Credentials). |
| `GCP_LOCATION` | `global` | Vertex AI location. |
| `LITELLM_BASE_URL` / `LITELLM_API_KEY` / `LLM_MODEL` | – / – / `gpt-5.4-mini` | Optional OpenAI-compatible endpoint, used for the multi-model comparison. |
| `LLM_DISABLED` | `false` | Set to `true` to force the labelled fallbacks everywhere. |
| `DATABASE_URL` | `sqlite:///var/inventory_rescue.db` | SQLAlchemy URL. Cloud SQL: `postgresql+psycopg://USER:PASS@/DB?host=/cloudsql/PROJECT:REGION:INSTANCE`. |
| `STORAGE_BACKEND` | `local` | `gcs` stores files in `GCS_BUCKET` (optional `GCS_PREFIX`). |
| `STATIC_DIR` | `frontend/dist` | Built UI served by FastAPI when present. |
| `EVAL_WORLDS` / `ROLLING_WORLDS` | `120` / `20` | Monte Carlo worlds for the robustness job. |
| `OPTION_WORLDS` | `10` | Calm worlds for the plan-options comparison (the same number of shock worlds is added). |
| `WORKING_CAPITAL_RATE` / `RESERVE_SHARE` / `LEAN_SAVINGS_SHARE` | `0.18` / `0.3` / `0.9` | Plan-option settings: annual cost of cash, share held back by "keep a reserve" (default; `POST /api/plans` accepts `reserve_share` 0.05–0.9 per plan), share of savings "lean" must keep. |

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
GEMINI_API_KEY=... PROJECT_ID=my-project PUBLIC=1 ./infra/gcp/deploy.sh   # use the AI Studio key instead of Vertex AI
```

The script is idempotent. It does the following:
1. Enables the required APIs.
2. Creates an Artifact Registry repo, a runtime service account (Vertex AI user, Cloud SQL client) and a private Cloud Storage bucket.
3. Creates a Cloud SQL PostgreSQL 16 instance, database and user. The generated `DATABASE_URL` goes into Secret Manager.
4. Builds the image with Cloud Build.
5. Deploys the Cloud Run service (region `asia-south1` by default) with `STORAGE_BACKEND=gcs`. With `GEMINI_API_KEY` set, the key goes into Secret Manager and the service uses the Gemini API (`LLM_PROVIDER=gemini`); otherwise it uses Gemini on Vertex AI through its service account (`LLM_PROVIDER=vertex`).
6. Creates and runs the `inventory-rescue-seed` Cloud Run job to load the demo data.

The service uses `--no-cpu-throttling` and `--min-instances 1`, because the robustness evaluation and forecast refresh run after the HTTP response returns. Later releases can use `gcloud builds submit --config cloudbuild.yaml` (or a trigger on `main`), which builds, pushes and rolls out the new image.

Local container check: `docker build -t inventory-rescue . && docker run -p 8080:8080 -e DATABASE_URL=... inventory-rescue`.

## 5-minute demo

**Simple view (2 minutes).** Open the app root.
1. Click **Read a message**, then **Show me an example**, then **Read it**. Answer **Yes, use this** to the plain-language event and wait for "Your numbers are up to date".
2. Next to the budget, choose how to buy: **Buy it all now**, **Buy in two steps** (part now, the rest at the weekly check only if still needed), **Keep money aside** (a slider sets how much to keep, 10–70%; sent as `reserve_share`) or **Spend less**. Each choice shows how much is paid now, later or kept, and the safest choice in the stress test is marked. Click **Make a new plan**: it shows the amount to spend, the saving compared with the usual reordering, and one card per product. Click **Approve and get my order list** to download the CSVs.
3. Click **← Back**, then **Ask a question**, and tap "Which products will run out first?".

**Advanced view.** Click **Advanced view** at the top right.

1. **Plan**: open `#/plan` and click **Generate plan** (₹10,00,000 budget). Review:
   - savings vs the tuned and static rules, fill rate and hero fill rate;
   - the action table with reasons;
   - the budget frontier.
   
   The robustness card fills in after about 10–20 seconds with Monte Carlo savings ranges, win rates, the weekly re-plan comparison and the plan-options results.
2. **Plan options**: in the "Plan options" card, compare cash paid today against loss in calm and shocked futures, then click **Use this option** on *Staggered buys* to see split buys (for example SKU-015: 4,776 units now and +2,568 ordered at the day-14 review).
3. **Inbox**: open `#/inbox`. Click a demo-message button (supplier delay for SUP-04, an influencer spike on SKU-019, an expiring cold-brew lot, a suppressed listing, a customs hold), then click **Extract signals**. Check each event's quoted evidence, then **Confirm** the delay and the spike. Scroll down to the **Extraction quality** card for precision/recall on the labelled set.
4. **Re-plan**: back on `#/plan`, click **Re-plan**. The "what changed" card lists the lines that moved and why: extra SKU-019 for the spike, and a workaround for the delayed POs. With signals confirmed, the robustness card also shows the plan-without-signals ablation.
5. **Override**: click **Edit plan**, change a quantity or discount or skip a line, and apply. The plan is re-scored and stays within budget.
6. **Approve**: click **Approve & export CSVs** and download the PO and markdown files.
7. **Assistant**: on `#/assistant`, ask "Why are we not expediting SKU-015?" or "What happens if SUP-04 slips another week?" and open the tool trace under the answer.
8. **SKU drill-down**: on `#/sku/SKU-019`, see the forecast band, the supplier-delay cases and the hero/standard priority toggle.
9. **Scenarios**: on `#/scenario`, try "SUP-04 delays another 7 days" or "budget is cut to 6 lakh", then click **Run backtest**.

## LLM safeguards

- The LLM only proposes. Every output is parsed as JSON and validated by code: schemas, known IDs, verbatim evidence, numeric bounds, forecast sanity checks and event reflection. Invalid output is never used silently.
- Extracted signals affect plans only after a human confirms them.
- Optimisation, simulation and money calculations are deterministic code. The LLM never chooses quantities.
- Explanations may only quote values that appear in the computed plan.
- The planning assistant can only call read-only tools with validated arguments (known SKU and supplier ids, bounded numbers). Previews and scenarios run in memory, and emails are drafts that are never sent. Every number, date and id in an answer must appear in that conversation's tool results or the question; after one failed correction the reply falls back to a labelled summary of the tool results.

## Known limitations

- The data is synthetic and covers a single warehouse. Value figures are simulated estimates. See the caveats under Results.
- On the AI Studio free tier (15 requests per minute), a full seed or backtest is throttled and slower; set `GEMINI_RPM=14`. Free-tier prompts may be used by Google to improve its products, so use a billing-enabled key for real company data.
- Background jobs run in-process. On Cloud Run this needs CPU always allocated, which `deploy.sh` sets. At larger scale, move them to Cloud Tasks or Cloud Run jobs.
- There is no user authentication in the app. Use the IAM-protected Cloud Run deployment (the default) for anything beyond a demo.
