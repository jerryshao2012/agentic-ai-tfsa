# TFSA Agent — Audit Log Reference

This document explains the structured JSON audit logs produced by the TFSA agent, so another
team can read, query, and reason about them without access to the source code.

It covers:
0. [The TFSA agent at a glance](#0-the-tfsa-agent-at-a-glance) — what the agent is and does
1. [What these logs are](#1-what-these-logs-are)
2. [The common envelope (fields on every line)](#2-the-common-envelope-fields-on-every-line)
3. [`event_type` catalog — what each event means](#3-event_type-catalog)
4. [How one user turn looks end-to-end](#4-how-one-turn-looks-end-to-end)
5. [How to read / query the logs](#5-how-to-read--query-the-logs)
6. [Field glossary](#6-field-glossary)

---

## 0. The TFSA agent at a glance

Before the log mechanics, here is *what is being logged*. The TFSA agent is a **multi-agent
LangGraph assistant** that answers questions about Canadian **Tax-Free Savings Accounts (TFSAs)**
and can execute contributions on the user's behalf. It is built as a **supervisor-routed graph**:
a single user turn enters one node, an LLM classifies what the user wants, and the request is
routed down one of a few specialist "lanes". Almost every field you'll see in the logs
(`intent`, `node`, `decision`, `data_selected`, `run_name`) comes directly from this structure.

### The nodes (each maps to a `node` / `run_name` in the logs)

| Node | Role |
|---|---|
| `profile_agent` | **Entry point.** Loads the user's profile and (in supervisor mode) runs the intent classifier. |
| `supervisor_router` | Logical classifier inside `profile_agent`. An LLM call that produces the `intent` and emits the `agent_reasoning` "classified intent=…" event. |
| `calculation_agent` | Computes available TFSA **contribution room** from the profile + CRA limits. |
| `document_agent` | Answers **policy** questions from a built-in CRA policy knowledge base. |
| `search_agent` | Performs a **live web search** (Tavily, with a DuckDuckGo fallback) when the static KB can't answer — e.g. current-year limits or post-2024 facts. |
| `transaction_agent` | **Executes a contribution** (the only state-changing lane). |
| `advisor_agent` | An **LLM tool-calling advisor** for open-ended financial guidance; it produces a complete answer and goes straight to `END`. |
| `response_agent` | Formats the final, polished answer for the policy / room / transaction lanes. |

### The four intents (the `intent` values you'll see)

The supervisor classifies every turn into exactly one of these, which decides the lane:

| `intent` | What the user wants | Lane (route) | `data_selected` |
|---|---|---|---|
| `policy` | A TFSA rules/policy question | `document_agent` → (maybe `search_agent`) → `response_agent` | `static_policy_kb` (→ `live_cra_search`) |
| `room` | "How much can I contribute?" | `calculation_agent` → `response_agent` | `user_profile+room_calc` |
| `contribute` | "Contribute $X for me" | `calculation_agent` → `transaction_agent` → `response_agent` | `user_profile+room_calc` |
| `advisory` | Open-ended advice / planning | `advisor_agent` → `END` | `advisor_tools` |

### Data sources (the `data_selected` values)

- `static_policy_kb` — the built-in CRA TFSA policy knowledge base.
- `user_profile+room_calc` — the user's loaded profile plus the contribution-room calculation.
- `live_cra_search` — a live web search for current/post-KB information.
- `advisor_tools` — the set of tools the advisor agent is allowed to call.

### Two router modes

`config.ROUTER_MODE` selects how routing is decided:
- **`supervisor`** — an LLM classifies the `intent` (the normal path; produces the
  `agent_reasoning` + `routing_decision` events described in §3d).
- **`rules`** — a deterministic regex fallback. This is also used automatically whenever the
  supervisor LLM is unavailable or returns something invalid, so you may see rule-based
  `routing_decision` reasons (e.g. `"matched contribution-room intent"`) even in supervisor mode.

> **Reading tip:** a `routing_decision` of `decision=document_agent, data_selected=static_policy_kb`
> is the policy lane; `decision=calculation_agent` then `transaction_agent` is someone actually
> contributing; `decision=advisor_agent` is the open-ended advisor. The rest of this document
> explains how each of those steps shows up as individual log lines.

---

## 1. What these logs are

Every line in the log files is **one self-contained JSON object** — a single "audit event".
There is no wrapping prefix, no multi-line records: each line can be parsed independently with
`json.loads(line)`.

These events are emitted by the agent's observability layer (`agent_obs.py`) and the agent
graph (`tfsa_assistant_graph.py`). They are written to **stdout** by the running agent, picked
up by the host runtime (AWS Bedrock AgentCore → CloudWatch Logs), and exported to **S3** — which
is where you are reading them.

Two important properties:

- **Observability never breaks the agent.** Every emit is wrapped defensively; a logging
  failure is swallowed, so a missing event does not mean the agent failed.
- **One event per fact.** A single LLM call produces a `llm_call_start` *and* a `llm_call_end`.
  A single user turn produces many events. You correlate them using the IDs in the envelope
  (see §2 and §4).

> **`event_type` is the single most important field** — it tells you *what kind of thing
> happened*. Everything else in this doc is organized around it.

---

## 2. The common envelope (fields on every line)

Every event — regardless of `event_type` — carries this same set of identifying fields. They
are how you filter, group, and stitch events into a coherent story.

| Field | Meaning |
|---|---|
| `ts` | UTC timestamp, ISO-8601 (e.g. `2026-06-07T21:00:29.945744+00:00`). When the event was emitted. |
| `event_type` | **The kind of event.** See the catalog in §3. |
| `agent` | Which agent emitted it. For this system it's `"tfsa"`. |
| `trace_id` | OpenTelemetry trace id (32 hex chars). Groups everything in **one distributed trace**. |
| `span_id` | OpenTelemetry span id (16 hex chars). The specific span the event fired in. |
| `session_id` | **Conversation id** — groups many turns/messages in one chat (e.g. `conv_000353`). |
| `message_id` | **One user turn / request.** All events for a single question share this. |
| `thread_id` | LangGraph thread id (may be `null`). |
| `user_id` | The end user, or `"unknown"` when not resolved. |

### How the IDs relate (most → least coarse)

```
session_id        one whole conversation (many user questions)
  └── message_id      one user question / turn (the unit you'll most often group by)
        └── trace_id      one execution trace through the agent graph
              └── run_id      one individual LLM or tool call (see §3)
```

- To see **everything for one conversation** → filter by `session_id`.
- To see **everything for one question the user asked** → filter by `message_id`.
- To **pair the start and end of a single LLM/tool call** → match on `run_id`.

---

## 3. `event_type` catalog

There are two families of events: **generic** (emitted automatically for any LLM/tool call) and
**domain** (emitted by the TFSA graph to explain its decisions). Below, "envelope fields" (§2)
are present on all of them and not repeated.

### 3a. Lifecycle of a user turn

| `event_type` | Meaning | Key extra fields |
|---|---|---|
| `invocation_start` | A user turn began. | `invocation_id`, `input` (the raw user question) |
| `invocation_end` | The turn finished. | `status` (`success`/`error`), `duration_ms`, `token_usage` (totals for the turn), `output` (final answer) |
| `invocation_error` | The turn failed with an exception. | `invocation_id`, `error`, `error_type` |

`invocation_id` equals the turn's `message_id`, so the start and end records join cleanly.

### 3b. LLM calls (the model thinking)

| `event_type` | Meaning | Key extra fields |
|---|---|---|
| `llm_call_start` | A call to the language model began. Captures the **full prompt**. | `run_id`, `parent_run_id`, `prompt` (the messages sent), `prompt_name`, `prompt_version`, `prompt_role`, `run_name`, `prompt_hash` |
| `llm_call_end` | The model returned. Captures the **raw completion** and token usage. | `run_id`, `duration_ms`, `completion` (the model's text output), `usage` (`input_tokens`/`output_tokens`/`total_tokens`), optionally `thinking`, optionally `tool_calls` |
| `llm_call_error` | The model call failed. | `run_id`, `error`, `error_type` |

- **Pair a start with its end** using `run_id`.
- `prompt_hash` is a short fingerprint (first 12 hex of SHA-256) of the prompt text — use it to
  detect prompt drift or tampering without diffing the full prompt.
- `prompt_name` / `prompt_version` tell you *which* prompt template and version produced the
  call (e.g. `document_policy_expert` `v2`).
- `tool_calls` (when present on `llm_call_end`) is **the model's decision to call a tool**:
  `[{"name": ..., "args": ...}]`. This is the agent "choosing" a tool.
- `thinking` appears only when extended/native reasoning was enabled for that call.

> In your sample, the `llm_call_end` completion is a JSON blob like
> `{"reasoning": "...", "intent": "policy"}` — that's the model classifying the user's question.

### 3c. Tool calls (the agent doing things)

| `event_type` | Meaning | Key extra fields |
|---|---|---|
| `tool_call_start` | A tool began executing. | `run_id`, `parent_run_id`, `tool` (name), `args` (inputs) |
| `tool_call` | A tool finished (success **or** error — check `status`). | `run_id`, `tool`, `status` (`success`/`error`), `duration_ms`, and either `result` (on success) or `error`+`error_type` (on failure) |

Pair `tool_call_start` and `tool_call` by `run_id` to get args + result + duration for one tool
execution.

### 3d. Domain events — the agent's reasoning & routing (TFSA-specific)

These explain *why* the agent did what it did. They exist because the routing/branching logic
would otherwise be invisible in the logs.

| `event_type` | Meaning | Key extra fields |
|---|---|---|
| `routing_decision` | The graph chose a branch / next node and which data source to use. This is the agent's **plan**. | `node` (where the decision was made), `decision` (the chosen next node), `reason` (why), `data_selected` (e.g. `static_policy_kb`) |
| `agent_reasoning` | A node's natural-language reasoning / classification. | `node`, `reasoning`, plus context such as `intent`, `needs_search` |
| `data_source` | Which data source backed a piece of data (e.g. a profile load). | `entity` (e.g. `profile`), `user_id`, `source` |
| `agent_node_output` | The text a graph node produced this step. | `node`, `content` |
| `node_error` | A specific node failed. | `node`, `error`, `error_type`, `stage` |

In your sample you can see the chain clearly:
1. `llm_call_end` → model classified `"intent": "policy"`.
2. `agent_reasoning` (node `supervisor_router`) → `"classified intent=policy"`.
3. `routing_decision` → `decision: document_agent`, `reason: supervisor intent=policy`,
   `data_selected: static_policy_kb`.
4. `llm_call_start` (`run_name: document_agent`, prompt `document_policy_expert` v2) → the
   policy expert prompt is sent to the model.

That is the agent deciding "this is a policy question, route it to the document agent backed by
the static policy knowledge base, and ask the policy-expert prompt."

---

## 4. How one turn looks end-to-end

For a single user question (one `message_id`), you will typically see events in this order:

```
invocation_start        user asked a question  (input = the question)
  llm_call_start        send classification prompt to the model
  llm_call_end          model returns intent (e.g. "policy")
  agent_reasoning       supervisor_router: "classified intent=policy"
  routing_decision      -> document_agent, data=static_policy_kb
  data_source           (if a profile/live data was loaded)
  llm_call_start        send the specialist prompt (e.g. document_policy_expert v2)
  llm_call_end          model returns the answer (+ token usage)
  tool_call_start       (if the model chose to call a tool, e.g. web search)
  tool_call             tool result + duration
  agent_node_output     node's produced text
invocation_end          status=success, total tokens, duration, final output
```

Not every turn has every event — e.g. a pure-policy question may use no tools, so there will be
no `tool_call`. A turn that fails will have `invocation_error` / `node_error` / `llm_call_error`
instead of (or alongside) the success events.

> **Note on duplicate-looking events:** the same logical turn can appear under more than one
> `trace_id` (e.g. retries or parallel spans). Use `message_id` + `run_id` to deduplicate, and
> watch `duration_ms` — in the sample one `llm_call_end` is `8088 ms` and a near-identical one
> is `76887 ms`, which is a latency signal worth flagging, not two different answers.

---

## 5. How to read / query the logs

### 5a. Each line is independent JSON

```bash
# Pretty-print every event
cat logs.jsonl | jq .

# Only the routing decisions
jq 'select(.event_type == "routing_decision")' logs.jsonl

# Everything for one conversation, in time order
jq 'select(.session_id == "conv_000353")' logs.jsonl

# Everything for one user turn
jq 'select(.message_id == "eb84d826-0e9f-4a12-93c0-710c9abfd9a4")' logs.jsonl

# All LLM calls with their latency and token totals
jq 'select(.event_type == "llm_call_end")
    | {ts, run_name, duration_ms, tokens: .usage.total_tokens}' logs.jsonl

# Count events by type
jq -r .event_type logs.jsonl | sort | uniq -c | sort -rn

# Find any errors
jq 'select(.event_type | test("error"))' logs.jsonl
```

### 5b. In CloudWatch Logs Insights (before S3 export)

Because every event has flat top-level fields, you can query directly:

```
fields ts, event_type, agent, node, decision, reason, data_selected
| filter event_type = "routing_decision"
| sort ts asc
```

```
fields ts, run_name, prompt_name, prompt_version, duration_ms, usage.total_tokens
| filter event_type = "llm_call_end"
| sort duration_ms desc
```

### 5c. Built-in inspection script

The repo ships a script that runs one real query and prints a readable per-turn summary
(routing decisions, tools called, model reasoning, LLM calls + tokens, final answer):

```bash
# from wxo_adk_external_agent/langgraph_python
./.venv/bin/python scripts/inspect_audit_events.py
./.venv/bin/python scripts/inspect_audit_events.py "How much room do I have? my user id is user123"
```

This is the fastest way to *see* the event flow for a single turn and is a good orientation
tool for the other team.

---

## 6. Field glossary

| Field | Appears on | Meaning |
|---|---|---|
| `ts` | all | UTC ISO-8601 emit time |
| `event_type` | all | the kind of event (§3) |
| `agent` | all | emitting agent (`tfsa`) |
| `trace_id` / `span_id` | all | OpenTelemetry trace / span ids |
| `session_id` | all | conversation id (many turns) |
| `message_id` | all | one user turn / request |
| `thread_id` | all | LangGraph thread (may be `null`) |
| `user_id` | all | end user, or `unknown` |
| `invocation_id` | invocation_* | id of the turn (= `message_id`) |
| `input` | invocation_start | raw user question |
| `output` | invocation_end | final answer text |
| `status` | invocation_end, tool_call | `success` / `error` |
| `duration_ms` | *_end, tool_call | wall-clock time of that call/turn, in ms |
| `token_usage` | invocation_end | total tokens for the whole turn |
| `usage` | llm_call_end | `{input_tokens, output_tokens, total_tokens}` for one call |
| `run_id` | llm_*/tool_* | id of one individual LLM or tool call (pair start↔end) |
| `parent_run_id` | *_start | parent call's `run_id` (call hierarchy) |
| `prompt` | llm_call_start | the messages sent to the model |
| `completion` | llm_call_end | the model's raw text output |
| `prompt_name` | llm_call_start | prompt template name (e.g. `document_policy_expert`) |
| `prompt_version` | llm_call_start | prompt template version (e.g. `v2`) |
| `prompt_role` | llm_call_start | role of the prompt (e.g. `system`) |
| `run_name` | llm_call_start | logical name of the call (e.g. `document_agent`) |
| `prompt_hash` | llm_call_start | 12-hex fingerprint of the prompt (drift/tamper check) |
| `thinking` | llm_call_end | native extended-reasoning text (when enabled) |
| `tool_calls` | llm_call_end | tools the model chose: `[{name, args}]` |
| `tool` | tool_call* | tool name |
| `args` | tool_call_start | tool inputs |
| `result` | tool_call | tool output (on success) |
| `node` | domain events | graph node where it happened |
| `decision` | routing_decision | the chosen next node |
| `reason` | routing_decision | why that route was chosen |
| `data_selected` | routing_decision | data source picked (e.g. `static_policy_kb`) |
| `intent` | agent_reasoning | classified user intent (`policy`/`room`/`contribute`/`advisory`) |
| `reasoning` | agent_reasoning | the node's natural-language rationale |
| `entity` / `source` | data_source | what was loaded and from where |
| `error` / `error_type` | *_error | exception message and class name |
| `stage` | node_error | pipeline stage where the error occurred |

---

### One-paragraph summary to hand the other team

> Each log line is one JSON "audit event" describing a single thing that happened inside the
> TFSA agent. The `event_type` field says *what* happened — a user turn starting/ending
> (`invocation_*`), the model being called (`llm_call_*`), a tool running (`tool_call*`), or the
> agent explaining its routing/reasoning (`routing_decision`, `agent_reasoning`, `data_source`).
> Every line also carries `session_id` (the whole conversation), `message_id` (one user
> question), and `run_id` (one model/tool call) so you can group related lines. To read a single
> question's full story, filter all lines with the same `message_id` and sort by `ts`.
