# Getnet Multi-Agent Support System

**[Leia em português](README.pt-BR.md)**

A multi-agent customer assistant for Getnet merchants. A **Router Agent** reads each message (with the conversation so
far) and returns a route plus a **plan**: one specialist, or two when the message mixes the merchant's own account with
a general rule. The specialists are a **Knowledge Agent** (RAG over Getnet's websites, measured retrieval, + web search) and a
**Customer Support Agent** (tools over the merchant's account data); a **Supervisor** merges their answers. A **Human
Escalation Agent** hands cases to people, and the human's reply comes back into the same conversation for the agent to
continue. Guardrails run before and after the agents, every step is traced, and it ships as one Docker service.

- **Language / stack:** Python 3.13, FastAPI, Google Gemini (LLM + embeddings), numpy vector search (+ optional BM25 hybrid), SQLite state, DuckDuckGo search
- **Agents:** Router (+ planner) · Knowledge · Customer Support · Supervisor · Human Escalation
- **Bonus features:** guardrails (direct and indirect injection), human hand-off with a reply loop, conversation memory,
  API-key identity, tracing, metrics, evaluation dataset + LLM-as-judge with a different model, CI

> **Version 2:** improvements update. §10 summarizes what changed.

---

## 1. Quick start

**Requirements:** Docker (with Compose) and a free Gemini API key from <https://aistudio.google.com/apikey>.

```bash
git clone <this-repo> && cd getnet-multi-agent
cp .env.example .env          # then edit .env: set GEMINI_API_KEY and ADMIN_API_KEY (staff key)
docker compose up --build
```

On the first start the service builds the knowledge base: from the committed snapshot
(`data/index_snapshot.json.gz`) when present, otherwise by crawling the Getnet websites (about 1–3 minutes; progress
appears in the logs). The index is stored in a Docker volume, so later starts are instant, and an index older than
`INDEX_MAX_AGE_DAYS` is refreshed in the background. `GET http://localhost:8000/health` returns `"status": "ok"` when the
knowledge base is loaded.

Send a message. The merchant is identified by its **API key**; the `user_id` in the body must match it (demo keys are in
§9):

```bash
curl -s -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer demo-key-cliente1988" \
  -d '{"message": "What is the difference between the Get Clássica and the Get Smart?", "user_id": "cliente1988"}'
```

The response includes a `session_id`. Send it back to continue the same conversation (memory):

```bash
curl -s -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
  -H "Authorization: Bearer demo-key-cliente1988" \
  -d '{"message": "And which one prints receipts?", "user_id": "cliente1988", "session_id": "<from the first answer>"}'
```

To run exactly the original contract (no key, `user_id` trusted from the body), set `AUTH_ENABLED=false`.

**Web console:** <http://localhost:8000/> — a small chat UI that shows, for every answer, which agent handled it,
the route and confidence, the tools called, the sources and a step-by-step trace with latencies. It also lets you
switch between the mock merchants and run the example scenarios with one click.

**Analytics dashboard:** <http://localhost:8000/dashboard> (staff key) — most asked questions, questions with the lowest
success rate, requests per day and per route, KPIs (success rate, user approval, escalation rate, latency p50/p95/p99,
estimated cost per question) and the latest evaluation results. Answers can be rated 👍/👎 in the console; see §7.

**Human agent inbox:** <http://localhost:8000/agent> (staff key) — the hand-off queue. A specialist reads the case
summary and the conversation and replies; the assistant delivers the reply in the customer's conversation and carries on
(the console shows it automatically). See §5.

Interactive API docs (Swagger UI): <http://localhost:8000/docs>

Run all the challenge's example scenarios and print a readable summary:

```bash
docker compose exec api python scripts/demo.py
```

### Useful commands

| Task | Command |
|---|---|
| Run the test suite (offline, no API key; separate test image) | `docker compose run --rm --build tests` |
| Run the evaluation suite (real LLM, judge = different model) | `docker compose exec api python -m evals.run_evals --judge` |
| Run the retrieval evaluation (dense vs. hybrid) | `docker compose exec api python -m evals.run_retrieval_evals` |
| Copy the reports out of the container (to commit them) | `docker compose cp api:/app/evals/report.md evals/` (also `report.json`, `retrieval_report.md`) |
| Rebuild the knowledge base (background, old index keeps serving) | `curl -X POST -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8000/admin/reindex` |
| Save the index as a committed snapshot | `docker compose exec api python -m app.rag.ingest --export-snapshot` then `docker compose cp api:/app/data/index_snapshot.json.gz data/` |
| Follow the structured logs | `docker compose logs -f api` |
| Operational metrics | `curl -H "X-Admin-Key: $ADMIN_API_KEY" localhost:8000/metrics` |

The latest committed reports are in [`evals/report.md`](evals/report.md) and
[`evals/retrieval_report.md`](evals/retrieval_report.md).

### Configuration

All settings are environment variables (see `.env.example`). The most relevant:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | – | Required |
| `LLM_MODEL` | `gemini-flash-lite-latest` | Model for the specialist agents |
| `ROUTER_MODEL` | `gemini-flash-lite-latest` | Model for routing |
| `LLM_THINKING_LEVEL` | `low` | Reasoning effort for thinking models |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | Embeddings for RAG (768 dims) |
| `MAX_TOOL_ITERATIONS` | `5` | Hard cap on each agent's tool loop |
| `ROUTER_CONFIDENCE_THRESHOLD` | `0.55` | Below this, the request goes to a human |
| `RETRIEVAL_TOP_K` / `RETRIEVAL_MIN_SCORE` | `5` / `0.45` | Retrieval breadth and relevance floor |
| `RETRIEVAL_MODE` | `dense` | `dense` or `hybrid` (dense + BM25, fused with RRF); default chosen by the retrieval eval |
| `ADMIN_API_KEY` | – | Staff key for `/dashboard`, `/agent`, `/metrics`, `/analytics`, `/evals`, `/handoffs` (empty = disabled) |
| `AUTH_ENABLED` | `true` | Identity from the API key; `false` = trust `user_id` from the body |
| `RATE_LIMIT_PER_MINUTE` | `20` | Per merchant and per client IP |
| `MEMORY_MAX_TURNS` / `SESSION_TTL_HOURS` | `6` / `24` | Turns given to the agents / idle time before a new session |
| `MAX_PLAN_STEPS` | `2` | Specialists the supervisor may run for one message |
| `JUDGE_MODEL` | `gemini-flash-latest` | Evaluation judge (a different model from the agents) |
| `INDEX_MAX_AGE_DAYS` | `7` | Older index is refreshed in the background |

The `-latest` aliases always resolve to the current Gemini release. To pin a version (recommended for production
reproducibility), set an exact model id from <https://ai.google.dev/gemini-api/docs/models>.

### Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements.lock -r requirements-dev.lock
cp .env.example .env   # set GEMINI_API_KEY and ADMIN_API_KEY
uvicorn app.main:app --reload
pytest
```

---

## 2. Architecture

```mermaid
flowchart TD
    U([POST /chat<br/>API key + message + session_id]) --> AU[Identity · rate limit<br/>user_id from the key]
    AU --> GI[Input guardrails<br/>injection · PII masking · size]
    GI -- blocked --> R0([Safe reply])
    GI --> MEM[(Conversation memory<br/>SQLite · last N turns)]
    MEM --> RT{{Router Agent<br/>route + confidence + plan}}
    RT -- greeting / out_of_scope / unsafe --> R1([Router's short reply])
    RT -- "plan: 1 or 2 steps" --> SUP[Supervisor<br/>runs the plan in order]
    RT -- escalation or low confidence --> EA[Human Escalation Agent]
    SUP --> SA[Customer Support Agent]
    SUP --> KA[Knowledge Agent]
    SA -- 2 answers --> SY[Supervisor synthesis]
    KA -- 2 answers --> SY
    KA -- escalate_to_human --> EA
    SA -- escalate_to_human --> EA
    KA --- KT[(search_knowledge_base<br/>web_search · untrusted data)]
    SA --- ST[(profile · sales & settlements<br/>transactions · terminal status<br/>support ticket · help center)]
    EA --- ET[(summary · priority<br/>HUM- ticket linked to the session)]
    EA -.-> HQ[[Human agent inbox /agent]]
    HQ -. reply .-> RES[Agent continues the conversation]
    RES -.-> MEM
    KA --> GO[Output guardrails]
    SA --> GO
    SY --> GO
    EA --> GO
    GO --> RESP([JSON: response · session_id · agent · route · sources<br/>tools_used · tickets · trace])
```

### The agents

| Agent | Responsibility | How it works | Tools |
|---|---|---|---|
| **Router** (+ planner) | Entry point. Decides who handles the message and in which order. | One LLM call with a JSON schema → `route`, `confidence`, `language`, `reason`, `reply`, `plan`. Sees the last turns of the conversation. Validated defensively: invalid output becomes a 0-confidence decision; the plan only accepts known specialists, no repeats, starting with the route. | – |
| **Knowledge** | Getnet products & services; general real-time questions. | Tool-calling loop. Searches the knowledge base first, falls back to the web, answers only from tool results. | `search_knowledge_base`, `web_search`, `escalate_to_human` |
| **Customer Support** | The merchant's own account: deposits, transactions, declines, card machines. | Tool-calling loop over the merchant's data; uses the help center for official procedures; opens tickets when troubleshooting is not enough. | `get_customer_profile`, `get_sales_and_settlements`, `get_recent_transactions`, `get_terminal_status`, `open_support_ticket`, `search_knowledge_base`, `escalate_to_human` |
| **Supervisor** | Combines the specialists when the plan has two steps. | Runs the steps in order with a shared context; if a step asks for a human, the remaining steps are dropped and the case is handed off (re-planning); with two answers, one synthesis call merges them using only the specialists' findings. | – |
| **Human Escalation** (bonus) | Hand-off to a human specialist. | LLM produces a case summary (with the whole conversation), priority and category, then a `HUM-` ticket is created, linked to the session, and the user gets a protocol number. One open hand-off per conversation. Falls back to a template if the LLM fails. | creates `HUM-` tickets |

### Communication between agents

The **orchestrator** (`app/orchestrator.py`) owns the workflow. Agents communicate through **direct, synchronous function
calls** that share one request-scoped `ToolContext` (user id, trace, collected sources, tickets, escalation flag).

- Specialists never call each other directly. When one decides a human is needed, it calls the `escalate_to_human`
  tool, which only raises a flag; the orchestrator then runs the Escalation Agent. All routing decisions stay in one place.
- Knowledge is shared through tools: the Support Agent uses the same `search_knowledge_base` tool as the Knowledge Agent,
  so "my machine won't connect" combines live diagnostics with the official help-center procedure.
- **Plans.** When a message needs two specialists ("my money from yesterday didn't arrive, and what is the D+1 rule?"),
  the Router returns `plan: [support, knowledge]` with a short task for each. The orchestrator runs them in order on the
  same context (the second step sees the sources and tickets of the first) and the Supervisor writes one answer.
  The plan is capped at `MAX_PLAN_STEPS`, so cost and latency stay bounded.
- **State** lives outside the agents: the conversation (SQLite, `app/memory.py`) is loaded before routing and written
  after the answer, so agents stay stateless and a restart loses nothing.

I chose direct calls over a message queue because every interaction is a short request/response: it is the easiest
model to trace, test and reason about. The human reply loop is asynchronous but needs no queue: the hand-off is
persisted, and the reply resumes the conversation from the stored state. A queue becomes worth it with long-running
agents (see §8).

### Message workflow example

`"When will the money from yesterday's sales be deposited?"` from `cliente1988`:

1. **Input guardrails:** no injection, no sensitive data → forwarded unchanged.
2. **Router:** `{"route": "support", "confidence": 0.95, "language": "en"}`.
3. **Support Agent, iteration 1:** the model calls `get_sales_and_settlements(period="yesterday")`. The tool reads
   **cliente1988's** data (the user id comes from the request, never from the model) and returns each sale with its
   expected deposit dates: debit D+1, Pix same day, credit D+30, installments every 30 days.
4. **Support Agent, iteration 2:** the model writes the answer from those numbers.
5. **Output guardrails:** masks any sensitive data the model might echo.
6. **Response:** answer + `session_id`, `agent`, `route`, `tools_used`, `sources`, `tickets` and the full `trace` with
   per-step latency and token usage. The turn is stored in the session.

`"Meu dinheiro de ontem não caiu e quero saber a regra do D+1"` (mixed question):

1. **Router:** `route: support`, `plan: [{support: "check yesterday's deposits"}, {knowledge: "explain the D+1 rule"}]`.
2. **Support Agent** reads the sales and expected deposit dates; **Knowledge Agent** searches the rule on the website.
3. **Supervisor** answers once: first the merchant's own deposits, then the rule, with the website sources.
4. `agent` = `support+knowledge+supervisor`, and the trace shows each step.

---

## 3. RAG pipeline

```
data/sources.txt ──► crawl ──► clean ──► chunk ──► drop poisoned chunks ──► embed ──► storage/index.json
                                                                                         │
user question ──► dense (cosine) [+ BM25 + RRF when hybrid] ──► relevance floor ──► wrapped as untrusted ──► LLM answer + sources
```

| Stage | Implementation | Why |
|---|---|---|
| **Ingestion** | `app/rag/ingest.py` crawls the seed URLs in `data/sources.txt` (getnet.net + site.getnet.com.br: card machines, Pix, crediário, payment link, digital account, loans, help center) and follows same-domain links one level deep (max 60 pages). Sequential, 0.5 s delay, identified User-Agent, public pages only. On getnet.net only the English edition is kept; legal, press and blog pages are skipped. | The challenge asked for sources beyond the suggested URL. Following one level of links catches product pages without hard-coding them all. getnet.net's country editions (Portugal, Spain, Mexico…) describe other markets' products and fees and would give Brazilian merchants wrong answers. |
| **Cleaning** | BeautifulSoup removes scripts, nav, header, footer, cookie banners; drops very short UI fragments and duplicate lines. | Menus and banners repeated on every page would otherwise dominate retrieval. |
| **Chunking** | Paragraph-aware, ~1000 characters with 150 overlap; the page title is prefixed to every chunk; duplicate chunks across pages are removed. | Paragraph boundaries keep ideas intact; overlap keeps answers that span a boundary; the title tells the model which product a chunk is about. |
| **Embedding** | `gemini-embedding-001`, 768 dimensions, `RETRIEVAL_DOCUMENT` for chunks and `RETRIEVAL_QUERY` for questions. | Multilingual: English questions retrieve Portuguese content. Asymmetric task types improve retrieval quality. |
| **Storage** | `app/rag/store.py`: unit-normalized vectors in numpy, persisted as JSON in a Docker volume. | For a few hundred chunks, exact brute-force search takes well under 1 ms and has no infrastructure to run. The interface (`add` / `search` / `save` / `load`) maps directly to Chroma, pgvector or Qdrant if the corpus grows. |
| **Retrieval (dense, hybrid optional)** | Dense ranking (cosine) + sparse ranking (BM25, `app/rag/bm25.py`, keeps tokens such as `d+1`) fused with **Reciprocal Rank Fusion** (k = 60), top-5. Relevance floor: cosine ≥ 0.45, or ≥ 0.35 for BM25's top 3 (strong exact match). Available with `RETRIEVAL_MODE=hybrid`; **the default is dense** because on the retrieval eval hybrid did not beat it on this corpus (Recall@5 0.93 for both; MRR@5 0.88 dense vs. 0.82 hybrid). | Embeddings capture meaning; BM25 catches exact product names and codes that embeddings blur. RRF uses ranks only, so the two score scales never need calibrating. Irrelevant chunks are worse than none: the floor keeps "nothing found" for unrelated questions. |
| **Untrusted content** | Instruction-like sentences are removed and every chunk / web snippet is wrapped in `<untrusted_content>`; prompts say tool results are data, never instructions. Chunks with injection patterns are dropped at ingestion. | Indirect prompt injection: a web page must not be able to give the agent orders. |
| **Freshness** | `built_at` in the index metadata; `/health` shows the age; older than `INDEX_MAX_AGE_DAYS` → background rebuild with an atomic swap (a failed crawl keeps the old index); `POST /admin/reindex` on demand. A committed snapshot makes the first start deterministic. | The website changes; the index must not silently go stale, and a refresh must never take the service down. |
| **Retrieval metrics** | `evals/run_retrieval_evals.py`: Recall@5 (hit rate), MRR@5, Precision@5 and empty rate, dense vs. hybrid, on 14 labelled queries. | Measures retrieval separately from generation, so a bad answer can be traced to "didn't find it" or "found it and misused it". |
| **Generation** | The Knowledge Agent calls `search_knowledge_base` (it may reformulate and search again), then answers only from the results. Sources are returned in a separate `sources` field. | Grounding rules live in the prompt; citations let users and evaluators check the answer. |

---

## 4. How the LLM's tools are used

- **Native function calling.** Each tool is a JSON schema (what the model sees) plus a Python function (what runs).
  The agent loop in `app/agents/base.py` runs tools manually instead of using the SDK's automatic mode, so every tool
  call is logged and traced with its arguments and result.
- **Structured output** (JSON schema) for the Router and the Escalation Agent: decisions are validated data, not text to parse.
- **Model choice is configuration:** router and agents can use different models. I benchmarked the larger Flash model against Flash-Lite on the example scenarios: with the knowledge base doing the heavy lifting, Flash-Lite gave equally grounded answers in ~7 s versus ~50 s (the larger model also hit 504 deadlines on the free tier), so it is the default. Thinking effort is set to `low` for the same reason.
- **Safety by construction:**
  - No tool accepts a `user_id` parameter. The id is injected from the request, so a prompt cannot make the model
    read another merchant's data.
  - Arguments the schema doesn't declare are dropped before execution.
  - Unknown tools and tool exceptions become error results the model can recover from; they never crash the request.
  - The loop is capped at `MAX_TOOL_ITERATIONS`. Hitting the cap escalates to a human instead of failing silently.
- **Provider abstraction.** Agents depend on the `LLMClient` protocol (`app/llm.py`), not on the Gemini SDK. Adding
  another provider means writing one class. The same seam lets the tests use a scripted fake model.
- **Read vs. write tools.** Tools that change something (`open_support_ticket`) are marked `side_effect=True`: at most
  one write per turn, and the write is **idempotent** — one open ticket per merchant + category in 24 h; a repeat returns
  the existing ticket (`deduplicated: true`). Hand-offs are idempotent per conversation.
- **Reliability:** request timeout, exponential backoff on 429/5xx errors and a **circuit breaker**: after 5 consecutive
  failures the LLM calls fail fast for 30 s (state shown in `/health`) instead of making every user wait for timeouts.

---

## 5. Guardrails and human hand-off (bonus)

**Identity and access** (`app/security.py`):

- the merchant is identified by its **API key** (`Authorization: Bearer`); keys are stored as sha256 hashes in
  `data/api_keys.json`. The `user_id` in the body must match the key (403 otherwise), so changing the body no longer reads
  another merchant's data. A message that names another merchant's account is refused before any LLM call.
  In production this is where Getnet's OAuth2/OIDC token would be validated;
- a session belongs to one merchant; feedback can only be given by the merchant who asked;
- operational and human-specialist endpoints require the **staff key** (`X-Admin-Key`, constant-time comparison);
- **rate limiting** per merchant and per IP (429 + `Retry-After`).

**Input** (`app/guardrails.py`, deterministic and tested):

- prompt-injection / jailbreak patterns (English and Portuguese) are blocked before any LLM call;
- **indirect injection:** content the agents read (knowledge base, web) is cleaned and wrapped as untrusted data (§3);
- card numbers (Luhn-validated, so order numbers are not affected), CVV, passwords, CPF and CNPJ are **masked before
  reaching the LLM or the logs**, and the user is told not to share them;
- empty and oversized messages are rejected.

**Semantic layer:** the Router labels `out_of_scope` (e.g. "write me a script") and `unsafe` requests and replies with
what the assistant *can* do.

**Output:** sensitive data is masked again in the final answer. Prompts forbid revealing instructions or inventing
fees, prices or data.

**Human hand-off** happens when:

- the user asks for a human or reports fraud, a chargeback or a legal issue (Router → `escalation`);
- the Router's confidence is below the threshold;
- a specialist calls `escalate_to_human`;
- an agent hits its iteration limit.

The Escalation Agent writes a case summary for the human team (with the whole conversation), sets priority and
category, creates a `HUM-` ticket linked to the session and tells the user the protocol number.

**Human-in-the-loop, the full loop:** agent → human → reply → agent continues.

1. The case appears in the **human agent inbox** (`/agent`, or `GET /handoffs`) with summary, priority and conversation.
2. The specialist replies (`POST /handoffs/{ticket}/reply`, optionally closing the case).
3. The reply is stored in the conversation and the specialist agent that had the case **writes the next message to the
   customer from it** (it may call tools to complement it). If the LLM fails, the human's reply is delivered as is.
4. The customer sees it in the same conversation (`GET /sessions/{id}/messages`; the console polls while a hand-off is
   open) and keeps talking: the next turn has the human's answer in its context.

---

## 6. Testing strategy

Testing an LLM system means separating **what must always be true** (the workflow) from **how good the model is**
(the answers). They need different tools.

| Layer | What it checks | Where | Needs API key? |
|---|---|---|---|
| **Unit** | Guardrails (masking, injection, Luhn), chunking, HTML cleaning, vector search and persistence, settlement-date logic, decline-code mapping, router output parsing | `tests/test_units.py` | No |
| **Integration (orchestration)** | Full flow with a scripted `FakeLLM`: correct agent per scenario, expected tools called, sources returned, escalation paths (explicit, low confidence, specialist-requested, loop cap), injection never reaching the LLM, PII masking end to end, web-search and LLM failures degrading gracefully, user-id isolation, trace and metrics recorded | `tests/test_orchestration.py` | No |
| **API contract** | Response schema, payload validation (422s), `/health`, `/metrics` | `tests/test_api.py` | No |
| **v2 behaviour** | Identity boundary (401/403, swapped `user_id`), staff endpoints, rate limit, memory across turns and restarts, session ownership, human reply loop, idempotent hand-offs and tickets, write limit, two-step plan + synthesis, re-planning to a human, indirect injection (retrieval, web, ingestion), official-domain preference, BM25/hybrid retrieval, circuit breaker | `tests/test_v2.py` | No |
| **Evaluation (quality)** | Real model on a labelled dataset: routing accuracy, tool-selection accuracy, key-content checks, LLM-as-judge groundedness and helpfulness, latency and tokens | `evals/` | Yes |

The `FakeLLM` (`tests/fakes.py`) implements the same interface as the Gemini client: it routes by keywords, calls the
tool a scenario needs, and answers by echoing tool results. That makes integration tests **deterministic, offline and
fast** (87 tests in about a second), so they run on every commit: **GitHub Actions** (`.github/workflows/ci.yml`)
installs the hash-locked dependencies, runs the suite, builds the test image and checks that the runtime image has no
tests or pytest inside. A failing integration test means the
orchestration broke, not that the model had a bad day.

**Comprehensive integration testing for the orchestration** extends this in four directions:

1. **Scenario matrix.** For every route × outcome (answered, tool error, empty retrieval, unknown customer, escalation),
   assert the agent path, the tool sequence and the response contract through the trace.
2. **Contract tests per tool.** Each tool's schema and output shape are pinned, so a change in the data layer cannot
   silently break what the model receives.
3. **Recorded real-model runs.** Record real Gemini responses once and replay them in CI (VCR-style). This catches
   regressions in prompt/response handling without paying for every run.
4. **Evaluation gates.** `evals/run_evals.py` runs on every prompt or model change. A drop in routing accuracy or
   groundedness versus the previous report blocks the change. The dataset grows with every production failure that
   gets fixed (regression tests for behaviour).

---

## 7. Observability, evaluation and production monitoring (bonus)

**What is implemented**

- **Traces:** each request gets a `trace_id`. Every step (guardrails, router decision, each LLM iteration, each tool
  call with arguments and result, escalation) is a span with its latency and token usage. The trace is returned in
  the API response and logged.
- **Structured logs:** one JSON line per span (`trace_id`, `user_id`, `span`, `duration_ms`, attributes), ready for
  Loki, ELK or Datadog. Sensitive data is masked before it reaches the logs.
- **Metrics:** `GET /metrics` exposes requests, errors, routes, guardrail blocks, escalations, tool-call counts,
  tokens, estimated cost, security flags (e.g. removed injection text) and p50/p95/p99 latency per route.
- **Evaluation:** a labelled dataset (`evals/dataset.jsonl`: the 10 challenge scenarios plus Portuguese, escalation,
  out-of-scope, injection, unknown-customer, **multi-turn memory**, **mixed (two-specialist)** and **adversarial** cases:
  a Portuguese jailbreak and a cross-merchant data request) and a runner that writes `evals/report.md` and
  `report.json` (committed) and the copy the dashboard shows, with the models and prompt version used.
  - Cases that expect no tools fail if any tool is called (refusals and hand-offs must not touch data).
  - The **judge is a different model** (`JUDGE_MODEL`, default the larger Flash) from the agents (Flash-Lite), and
    receives the **full** tool outputs of each turn. A judge from another provider, and a monthly human review of a
    sample of judged answers to measure agreement, are the next calibration steps.
- **Cost:** tokens per request and an estimated cost (list prices, configurable) in `/metrics` and the dashboard.
- **Conversation analytics** (`app/analytics.py`, SQLite on the storage volume): every request is stored with its
  outcome (masked message only). A question's **success** is the user's 👍/👎 when rated; otherwise **automatic
  resolution** — answered without error, escalation or guardrail block, and grounded (sources returned or the
  merchant's data consulted). `GET /dashboard` shows the most asked questions, the lowest success rates and the
  trends, so the team knows which topics need better content or prompts.

**How I would run it in production**

| Signal | Tooling | Alert when |
|---|---|---|
| Latency p95, error rate, LLM 429/5xx | OpenTelemetry → Prometheus / Grafana | p95 > 8 s or error rate > 2% for 5 min |
| Cost | tokens per request and per route | daily spend over budget, or tokens/request jumps (loops, prompt bloat) |
| Routing health | route distribution, router confidence histogram | sudden shift in distribution or many low-confidence routes |
| Escalation rate | escalations / requests | > X% (the assistant is failing) or ≈ 0 (hand-off broken) |
| Retrieval quality | top-1 similarity, % empty retrievals | empty-retrieval rate rises (stale index or site changed) |
| Answer quality | nightly LLM-as-judge on a sample of real conversations; thumbs up/down | groundedness score drops |
| Safety | guardrail blocks by type | spike in injection attempts |

Agent-level tracing tools such as Langfuse or LangSmith fit directly on top of the existing spans.

---

## 8. Design decisions, trade-offs and limitations

- **No agent framework.** The orchestration is about 300 lines of plain Python. With five agents and a clear flow, a
  framework would add abstraction without adding capability, and every step here is explicit and testable. LangGraph
  would be my choice if the flow became a real graph with loops between agents; the human pause/resume and persistent state needed here fit in a small state store.
- **Gemini.** Free tier, and one provider for both the LLM and embeddings. The `LLMClient` abstraction keeps the choice reversible.
- **numpy vector store** instead of a vector database: exact, fast and zero-ops at this size (see §3).
- **DuckDuckGo search** needs no API key. Web results (weather, exchange rates) are approximate snippets, and the agent
  says where they came from. In production I would use dedicated APIs for weather and exchange rates.
- **Mock customer data.** `data/customers.json` is fictitious and uses relative dates, so "yesterday's sales" always
  works. Settlement rules are simplified (business days without holidays).
- **Memory in SQLite.** Right for one replica; with several replicas, sessions and hand-offs move to Postgres/Redis
  behind the same `ConversationStore` interface. The same applies to the JSON ticket file and the in-memory rate limiter.
- **Plans are sequential and capped at two steps.** Enough for the real mixed questions; a general planner (loops between
  agents) is where a graph framework such as LangGraph would start to pay off.
- **Crawl freshness.** Age-based refresh. In production a scheduled job would compare page hashes and re-embed only what changed.

**Production evolution:** OAuth2/OIDC instead of API keys, Postgres/Redis for state, a queue for long-running agents, a
managed vector database with incremental re-ingestion, recorded-response CI, a judge from another provider with human
calibration, and a canary evaluation before every prompt or model release.

---

## 9. Project structure

```
app/
  main.py               FastAPI app: /chat, /sessions, /feedback, /handoffs, /agent, /dashboard, /analytics, /evals, /health, /metrics
  security.py           API-key identity, staff key, rate limiting
  memory.py             Conversation state: sessions, turns, human hand-offs (SQLite)
  resilience.py         Circuit breaker for the LLM provider
  analytics.py          Conversation analytics: request log, feedback, success rate per question
  static/index.html     Web console (chat + agent path, tools, sources, trace, 👍/👎, human replies)
  static/dashboard.html Analytics dashboard
  static/agent.html     Human agent inbox (hand-off queue + reply)
  orchestrator.py       Workflow: guardrails → memory → router/plan → specialists → supervisor → escalation → output
  config.py             Settings (environment variables)
  llm.py                LLMClient interface + Gemini implementation (retries, timeouts)
  guardrails.py         Input/output guardrails
  observability.py      Traces, structured logs, metrics
  agents/
    prompts.py          All system prompts and output schemas (versioned)
    base.py             Tool-calling agent loop
    router.py           Router Agent
    specialists.py      Knowledge, Customer Support, Supervisor and Human Escalation agents
  tools/                Tool definitions (knowledge, support, escalation)
  rag/                  ingest.py (crawl → chunk → embed) · store.py · bm25.py · retriever.py (hybrid + RRF)
  data/                 customer_repository.py (mock data access + tickets)
data/
  sources.txt           Seed URLs for the knowledge base
  customers.json        Fictitious merchants (cliente1988, cliente2024)
  api_keys.json         Hashed demo API keys
tests/                  Unit, integration, API and v2 tests (offline, fake LLM)
evals/                  Datasets, runners and the committed reports (report.md, retrieval_report.md)
.github/workflows/      CI: tests + Docker images
requirements.lock       Hash-pinned dependencies (requirements-dev.lock for pytest)
scripts/demo.py         Runs the challenge scenarios against the API
```

Mock users for testing: **`cliente1988`** (bakery in Porto Alegre, Get Smart offline with weak Wi-Fi, declined
transactions with codes 51 and 54, sales yesterday in debit, credit, 3× installments and Pix) and **`cliente2024`**
(auto parts store, automatic advance enabled). Any other id behaves as an unknown customer.

Demo API keys (fictitious, only for this challenge): `demo-key-cliente1988`, `demo-key-cliente2024` and
`demo-key-unknown` (a valid login with no merchant account). The console signs in with them automatically.

---

## 10. Version history

| Area | v1 | v2 | Where |
|---|---|---|---|
| Orchestration | Router → one specialist | Router returns a plan (1–2 specialists); supervisor runs it and merges the answers; a step that needs a human re-plans to a hand-off | `orchestrator.py`, `agents/router.py`, `agents/specialists.py` |
| Memory | Single-turn | Sessions and turns in SQLite on the volume; last N turns given to router and specialists; survives restarts | `memory.py` |
| Human hand-off | Ends in a `HUM-` ticket | Human agent inbox + reply endpoint; the reply goes into the conversation and the agent continues | `orchestrator.resume_after_human`, `static/agent.html` |
| Identity and access | `user_id` from the body | API-key identity (body must match), staff key on operational endpoints, rate limiting | `security.py`, `main.py` |
| Retrieval | Dense only, unmeasured | Retrieval metrics (Recall@5 / MRR@5 / Precision@5); BM25 + RRF hybrid implemented and measured against dense (dense kept as default) | `rag/bm25.py`, `rag/retriever.py`, `evals/run_retrieval_evals.py` |
| Indirect injection | – | Web/knowledge content cleaned and wrapped as untrusted; poisoned chunks dropped at ingestion; official domains preferred | `guardrails.py`, `tools/knowledge_tools.py`, `rag/ingest.py` |
| Index freshness | Built once | Age in `/health`, background refresh with atomic swap, `/admin/reindex`, snapshot support | `main.py`, `rag/ingest.py` |
| Evaluation | 15 cases, same-family judge | 20 cases (multi-turn, mixed, adversarial), judge on a different model, stricter tool check, committed reports | `evals/` |
| Write tools | No limits | `side_effect` tools, one write per turn, idempotent tickets and hand-offs | `tools/base.py`, `data/customer_repository.py` |
| Reliability and cost | Retries | Circuit breaker, estimated cost, p99 | `resilience.py`, `observability.py` |
| Build and CI | Single-stage image, unpinned hashes | Hash-locked dependencies, multi-stage image without tests, GitHub Actions | `Dockerfile`, `requirements*.lock`, `.github/workflows/ci.yml` |

---

## Development notes

I built this project with Claude Opus 5.5 as a programming assistant. I used Gemini as the LLM and VirtualBox for
virtualization: everything runs inside an Ubuntu Server LTS virtual machine, in a Docker container.

I set up port forwarding to reach the web interface from the physical machine's browser, and SSH from PowerShell to
manage the VM through its command line.

I ran into a few problems, such as Gemini timeouts because the free tier didn't perform as expected (solved by moving to
the paid tier), and RAG issues with pages in the same language but from another country, such as Portugal (I fixed it by
limiting which pages are crawled).

I also tried switching the RAG search from dense to hybrid, but it scored worse in the evaluation, so I went back to
dense search, which scored better.
