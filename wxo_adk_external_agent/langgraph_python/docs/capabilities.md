# TFSA Agentic AI — Capabilities

A LangGraph multi-agent assistant for Canadian Tax-Free Savings Account (TFSA) questions and
contributions. It answers policy questions, calculates personal contribution room, executes
contributions, and handles advisory/what-if questions — with structured audit logging throughout.

## Flow (one entry, four lanes)

```
                          profile_agent  (entry; runs SUPERVISOR intent classifier)
                                 │  intent = policy | room | contribute | advisory
        ┌────────────────┬───────┴───────────┬────────────────────┐
     policy            room               contribute            advisory
        │                │                    │                     │
   document_agent   calculation_agent   calculation_agent      advisor_agent
        │                │                    │                  (LLM picks tools)
  (search_agent          │              transaction_agent            │
   if needs_search)      │                    │                      │
        └──────┬─────────┴────────────────────┘                      │
            response_agent  (synthesizes final answer)               │
                 │                                                    │
                END ◄───────────────────────────────────────────────┘
```

**Lanes**
- **Policy:** `profile → document_agent → [search_agent if needed] → response_agent → END`
- **Room:** `profile → calculation_agent → response_agent → END`
- **Contribute:** `profile → calculation_agent → transaction_agent → response_agent → END`
- **Advisory:** `profile → advisor_agent → END` (the advisor is itself an LLM agent that produces a
  complete answer)

## Agents / nodes

| Node | LLM? | Role |
|---|---|---|
| `profile_agent` | LLM (router) | Entry. Classifies intent via the supervisor router; does **not** fetch a profile |
| `document_agent` | LLM | Answers policy questions from S3-loaded limits + known rules; flags if live search is needed |
| `search_agent` | LLM | Live CRA web search (Tavily → DuckDuckGo) + synthesis, when current data is needed |
| `calculation_agent` | deterministic | Computes contribution room (pure Python math) |
| `transaction_agent` | deterministic | Validates room/balance + executes a contribution |
| `advisor_agent` | LLM (ReAct) | Advisory/compound/what-if questions — the LLM **selects its own read-only tools** |
| `response_agent` | LLM | Final-answer synthesis for the policy/room/contribute lanes |

## Tools

**Action tools (invoked by specific nodes in Python):**

| Tool | Used by | What it does |
|---|---|---|
| `retrieve_user_profile` | calc / transaction lanes | Load profile from S3 (built-in mock fallback) |
| `search_cra_tfsa_policy` / `search_cra_tfsa_policy_duck_duck_go` | `search_agent` | Live CRA web search with Tavily → DuckDuckGo failover |
| `execute_tfsa_contribution` | `transaction_agent` **only** | Moves money — gated to the deterministic lane |

**Advisor read-only tools (the LLM chooses among these in `advisor_agent`):**

| Tool | What it does |
|---|---|
| `get_tfsa_room(user_id)` | Available room + breakdown (shares the calc with `calculation_agent`) |
| `get_transaction_history(user_id)` | Past contributions / withdrawals |
| `lookup_tfsa_limit(year)` | One year's annual contribution limit |
| `simulate_withdrawal(user_id, amount)` | "What if I withdraw X?" (re-added to room next calendar year) |
| `project_future_room(user_id, years)` | Project available room forward |

> Money movement (`execute_tfsa_contribution`) is intentionally **not** an advisor tool — only the
> deterministic transaction lane can move money.

## What it can answer
- **Policy:** annual limits (any year, from the data source), over-contribution penalties,
  eligibility, deadlines, withdrawal re-contribution rules.
- **Personal room:** "how much can I contribute?" (requires a user id — see below).
- **Contributions:** "contribute $500" → validated against room/balance, then executed.
- **Advisory / what-if:** "what's my room and should I top up before year-end?",
  "what if I withdraw $5k?", "how much room will I have in 2 years?" — LLM-planned with tools.
- **Multi-turn:** remembers `user_id` / context per `thread_id`.

## Passing a user id
Identity is currently parsed from the message text (demo mechanism). Accepted phrasings
(case-insensitive): `my user id is <id>`, `user id: <id>`, `id = <id>`, `user id <id>`
(e.g. `my user id is user_123`). Once provided with a `thread_id`, it persists across turns.

> Note: this is caller-supplied and unauthenticated — fine for a demo, **not** for production.
> Real deployments must bind identity to a verified session (SSO/JWT).

## Data sources
S3 bucket `aws-hackathon-team2` → `profiles/`, `transactions/`, `tfsa_limits.json`. Falls back to
a built-in mock profile + local `tfsa_limits.json` when S3 is unavailable (e.g. no credentials).
Every profile load emits a `data_source: s3|mock` audit event so you can tell real data from the
fallback.

## Routing modes (`ROUTER_MODE`)
- **`supervisor`** (default): an LLM classifies intent (`policy|room|contribute|advisory`). More
  robust to phrasing; adds one LLM call per turn.
- **`rules`**: deterministic regex router (no extra LLM call). Also the automatic fallback if the
  supervisor LLM call fails.

## Observability (logged per turn)
Structured JSON events on the `agent.audit` logger:
`invocation_start` / `invocation_end` · `routing_decision` (which branch + why) ·
`agent_reasoning` · `llm_call_start` / `llm_call_end` (prompt identity, tokens, **chosen
`tool_calls`**, native `thinking` when `ENABLE_THINKING=true`) · `tool_call_start` / `tool_call`
(name, args, result, duration) · `data_source` (s3|mock) · `node_error`.

Inspect any turn locally:
```
./.venv/bin/python scripts/inspect_audit_events.py "my user id is user_123, what's my room?"
```

## Reliability
- Concurrency-safe: blocking work is offloaded off the event loop; the TFSA limits refresh uses a
  double-checked lock + atomic file write.
- LLM calls have bounded timeouts + retries (with jittered backoff) and never return an empty
  reply — failures fall back to a useful message.
- Web search fails over Tavily → DuckDuckGo and tolerates Tavily's non-raising error payloads.

## Configuration (env)
| Var | Purpose |
|---|---|
| `AI_SERVICES_PROVIDER` | `bedrock` \| `watsonxai` \| `openai` \| `deepseek` \| `ollama` |
| `BEDROCK_MODEL_ID`, `AWS_REGION` | Bedrock model + region |
| `BEDROCK_MAX_ATTEMPTS`, `BEDROCK_READ_TIMEOUT`, `BEDROCK_MAX_TOKENS` | Bedrock client resilience |
| `LLM_INVOKE_ATTEMPTS` | App-level LLM retry count |
| `ROUTER_MODE` | `supervisor` (default) \| `rules` |
| `ENABLE_THINKING` | Log native model reasoning on `llm_call_end` (Bedrock/Claude) |
| `DATA_S3_BUCKET`, `PROFILE_S3_PREFIX`, `TRANSACTIONS_S3_PREFIX`, `LIMITS_S3_KEY`, `DATA_S3_REGION` | S3 data source |
| `TAVILY_API_KEY` | Tavily search (falls back to DuckDuckGo if absent/over quota) |

## Known limitations
- **Identity is caller-supplied** (parsed from text), not authenticated — demo only.
- **Room accrual** starts at the profile's `first_tfsa_year`; if that field means "account-opened
  year" rather than "first eligible year", room is understated for users eligible before they
  opened an account (TFSA room accrues from age-18 / 2009 onward).
- **`execute_tfsa_contribution` is a mock** banking call — no real transfer, confirmation step,
  idempotency, or fraud checks yet.
