# tfsa_assistant_graph.py
import asyncio
import datetime
import hashlib
import json
import logging
import mlflow
import operator
import os
import random
import re
import tempfile
import threading
import time
import uuid
from langchain.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph
from typing import AsyncGenerator, TypedDict, Annotated, Optional

import config
import data_sources
from cache import Cache
from models import ModelName, DEFAULT_MODEL

# Optional OpenTelemetry support for AWS CloudWatch observability
try:
    import otel_utils as otel
except ImportError:
    otel = None


def trace_node(name: str):
    """Decorator to trace a graph node using OpenTelemetry if available."""
    if otel and hasattr(otel, "traced"):
        return otel.traced(name)
    return lambda fn: fn


# Structured audit logging (tool calls, full LLM prompt/completion, token usage) for
# CloudWatch -> S3. Agent-agnostic framework; this graph is its first consumer.
from agent_obs import AuditCallbackHandler, audited_run, log_event

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


def _should_write_graph_image() -> bool:
    """Return False when graph image output is disabled via environment flag."""
    return os.getenv("TFSA_SKIP_GRAPH_IMAGE", "0").strip().lower() not in {"1", "true", "yes", "on"}


# Reduces call center volume by 80%+
# Processes contributions in <2 seconds
# Ensures 100% compliance with CRA regulations
# Provides personalized financial guidance
# TODO: Withdrawal simulation agent
# TODO: Contribution optimization advisor
# TODO: Multi-year projection tool
# TODO: Integrated tax impact analysis

def _bedrock_runtime_client():
    """A bedrock-runtime client with adaptive retries + timeouts.

    Adaptive mode adds client-side rate limiting and more retry attempts than the default
    "legacy" mode (4), which is the main reason ThrottlingException was surfacing as
    "No response generated" under load.
    """
    import boto3
    from botocore.config import Config
    return boto3.client(
        "bedrock-runtime",
        region_name=config.AWS_REGION,
        config=Config(
            retries={"max_attempts": config.BEDROCK_MAX_ATTEMPTS, "mode": "adaptive"},
            read_timeout=config.BEDROCK_READ_TIMEOUT,
            connect_timeout=10,
        ),
    )


def initialize_llm():
    """Initializes and returns the appropriate LLM based on configuration."""
    provider = config.AI_SERVICES_PROVIDER

    if 'bedrock' in provider:
        # ChatBedrockConverse uses Bedrock's Converse API, which is required for Amazon Nova
        # and also works for Anthropic Claude. It streams natively via astream_events.
        try:
            from langchain_aws import ChatBedrockConverse
            return ChatBedrockConverse(
                client=_bedrock_runtime_client(),
                model=config.BEDROCK_MODEL_ID,
                temperature=0,
                max_tokens=config.BEDROCK_MAX_TOKENS,
            )
        except ImportError:
            raise ValueError(
                "Bedrock provider selected but 'langchain[aws]' not installed. "
                "Run: pip install 'langchain[aws]' bedrock-agentcore"
            )

    if 'ollama' in provider:
        from langchain_ollama import ChatOllama
        return ChatOllama(model=ModelName.ollama_qwen2_5vl_7b, temperature=0)

    if 'deepseek' in provider:
        from langchain_deepseek import ChatDeepSeek
        return ChatDeepSeek(model=ModelName.deepseek_chat, temperature=0, api_key=config.DEEPSEEK_API_KEY)

    if 'openai' in provider:
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=ModelName.openai_gpt_4_o_mini, temperature=0, streaming=False,
                          api_key=config.OPENAI_API_KEY)

    # Default to Watsonx.ai
    from ibm_watson_machine_learning.foundation_models import Model
    from ibm_watson_machine_learning.metanames import GenTextParamsMetaNames as GenParams

    class WatsonLLM:
        def __init__(self):
            watsonx_params = {
                GenParams.DECODING_METHOD: "greedy",
                GenParams.MIN_NEW_TOKENS: 1,
                GenParams.MAX_NEW_TOKENS: 1024,
                GenParams.TEMPERATURE: 0,
            }
            self.model = Model(
                model_id=ModelName.watsonx_llama_3_2_90b,
                params=watsonx_params,
                credentials={"apikey": config.WATSONX_API_KEY, "url": config.WATSONX_URL},
                project_id=config.WATSONX_PROJECT_ID
            )

        def invoke(self, prompt: str) -> str:
            """Invoke Watsonx model with prompt and return response"""
            return self.model.generate_text(prompt)

    return WatsonLLM()


def initialize_thinking_llm():
    """Optional Claude-on-Bedrock instance with native extended thinking enabled.

    Returns None unless config.ENABLE_THINKING is set AND the provider is Bedrock. Extended
    thinking requires temperature=1 and max_tokens > budget_tokens, so this is a SEPARATE
    instance from the deterministic temperature=0 `llm` used for strict JSON parsing.
    """
    if not config.ENABLE_THINKING or 'bedrock' not in config.AI_SERVICES_PROVIDER:
        return None
    try:
        from langchain_aws import ChatBedrockConverse
        return ChatBedrockConverse(
            client=_bedrock_runtime_client(),
            model=config.BEDROCK_MODEL_ID,
            temperature=1,  # required by Anthropic extended thinking
            max_tokens=config.THINKING_MAX_TOKENS,
            additional_model_request_fields={
                "thinking": {"type": "enabled", "budget_tokens": config.THINKING_BUDGET_TOKENS}
            },
        )
    except Exception as e:  # never break startup over an optional debugging feature
        logging.warning("Thinking-enabled LLM unavailable (%s); falling back to standard llm", e)
        return None


llm = initialize_llm()
thinking_llm = initialize_thinking_llm()

# Stable identifiers for each agent's system prompt, surfaced on llm_call_start events
# (prompt_name/prompt_version/prompt_role/prompt_hash) so prompts are queryable and diffable
# in the logs. Bump the version string whenever a prompt's text is changed.
PROMPTS = {
    "document_agent": ("document_policy_expert", "v2"),
    "search_agent": ("search_synthesis", "v2"),
    "response_agent": ("response_specialist", "v2"),
}


def _prompt_config(agent: str) -> dict:
    """LangChain invoke config that tags an LLM call with its prompt identity.

    Passed as ``llm.invoke(prompt, config=_prompt_config("document_agent"))``; the audit
    callback reads run_name + metadata and records them on the llm_call_start event.
    """
    name, version = PROMPTS.get(agent, (agent, "v1"))
    return {
        "run_name": agent,
        "metadata": {"prompt_name": name, "prompt_version": version, "prompt_role": "system"},
    }


def _extract_string_content(content) -> str:
    """Safely extracts string content from message content, which can be a string or a list."""
    if not content:
        return ""
    if isinstance(content, list):
        text_chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text_chunks.append(block.get("text", ""))
            elif isinstance(block, str):
                text_chunks.append(block)
        return "".join(text_chunks).strip()
    return str(content).strip()


# Matches a complete <thinking>...</thinking> (or <think>...</think>) block, an unclosed
# (truncated) opening block, and bare tags. Some models — notably Amazon Nova — emit their
# chain-of-thought as literal <thinking> text in the reply rather than as a separate content
# block, so it must be stripped from anything user-facing and kept in logs only.
_THINKING_BLOCK_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*?</think(?:ing)?\s*>", re.DOTALL | re.IGNORECASE)
_THINKING_OPEN_RE = re.compile(r"<think(?:ing)?\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)
_THINKING_TAG_RE = re.compile(r"</?think(?:ing)?\b[^>]*>", re.IGNORECASE)


def _split_thinking(text: str) -> tuple[str, str]:
    """Separate a model's literal <thinking> reasoning from the user-facing reply.

    Returns ``(clean_text, thinking_text)``. Handles multiple blocks and a dangling,
    unclosed block (model cut off mid-reasoning). ``thinking_text`` is the concatenated
    inner reasoning, intended for LOGS ONLY — it must never be shown to the user.
    """
    if not isinstance(text, str) or "<think" not in text.lower():
        return (text or ""), ""
    blocks = _THINKING_BLOCK_RE.findall(text)
    clean = _THINKING_BLOCK_RE.sub("", text)
    open_match = _THINKING_OPEN_RE.search(clean)  # leftover unclosed <thinking> ... EOF
    if open_match:
        blocks.append(open_match.group(0))
        clean = _THINKING_OPEN_RE.sub("", clean)
    thinking = "\n".join(_THINKING_TAG_RE.sub("", b).strip() for b in blocks).strip()
    return clean.strip(), thinking


def _stream_safe_clean(raw: str) -> str:
    """Clean text safe to emit so far while streaming, holding back a trailing partial tag.

    `raw` is the full accumulated raw stream. Strips finished/open <thinking> blocks, then
    withholds any trailing fragment that could be the start of a tag (e.g. ``<``, ``</thi``)
    so a tag split across chunks is never emitted before it's resolved.
    """
    clean, _ = _split_thinking(raw)
    partial = re.search(r"<\/?[a-zA-Z]*\Z", clean)
    if partial:
        clean = clean[:partial.start()]
    return clean


def _extract_amount_from_text(user_input: str) -> float:
    """Extract a dollar amount from free text; returns 0.0 when absent/invalid.

    Prefers an explicit ``$``-prefixed amount. Otherwise takes a standalone number that is
    NOT embedded in a word/identifier — so e.g. the "123" in "user_123" is never mistaken for
    the amount (that bug once let "contribute $500 ... user_123" transact $123).
    """
    number = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
    # 1) Explicit dollar amount, e.g. "$2,000" / "$500.50".
    match = re.search(rf"\$\s*({number})", user_input)
    if not match:
        # 2) A standalone number not preceded/followed by word chars (avoids "user_123").
        match = re.search(rf"(?<![\w.])({number})(?!\w)", user_input)
    if not match:
        return 0.0
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return 0.0


def _is_hypothetical_or_probe(user_input: str) -> bool:
    """Detect prompts that discuss actions hypothetically vs requesting execution."""
    probe_pattern = (
        r"\bhypothetical\b|\bsuppose\b|\bwhat if\b|\bimagine\b|\bsimulate\b|\btest\b|"
        r"\bsafeguard\b|\bbypass\b|\bwithout (?:verification|confirmation)\b|"
        r"\bvulnerab|\bunauthoriz|\bsecurity\b|\bprotocol\b|\bcould the bot\b"
    )
    return bool(re.search(probe_pattern, user_input, re.IGNORECASE))


def _is_transaction_request(user_input: str) -> bool:
    """Return True only for concrete first-person transfer intents, not policy questions."""
    text = user_input.strip().lower()
    if not text:
        return False

    has_action_verb = bool(re.search(r"\b(contribute|deposit|transfer|add|invest)\b", text))
    if not has_action_verb:
        return False

    if _is_hypothetical_or_probe(text):
        return False

    # Questions about capabilities/policies should not trigger transaction execution paths.
    if "?" in text and not bool(re.search(r"\b(please|go ahead|proceed|execute|do it|i want to)\b", text)):
        return False

    # Require either explicit execution wording or an amount + action verb.
    has_execute_phrase = bool(re.search(r"\b(please|go ahead|proceed|execute|do it|i want to)\b", text))
    has_amount = _extract_amount_from_text(text) > 0
    return has_execute_phrase or has_amount


def _build_rate_limit_fallback(user_input: str, current_year: int) -> str:
    """Return a useful deterministic response when provider throttling occurs."""
    text = (user_input or "").lower()

    if re.search(r"eligib|age|resident|sin", text):
        body = (
            "Quick TFSA eligibility guide:\n"
            "* You must be 18+ and a Canadian resident to contribute.\n"
            "* A valid SIN is required.\n"
            "* Unused room carries forward; non-resident contribution years can trigger penalties."
        )
    elif re.search(r"deadline|timing|when", text):
        body = (
            "Quick TFSA timing guide:\n"
            "* Contributions can be made any time in the calendar year.\n"
            "* Withdrawals are added back on Jan 1 of the following year.\n"
            "* Over-contributions are penalized at 1% per month on the excess."
        )
    elif re.search(r"contribution room|how much|available room|limit available", text):
        body = (
            f"To calculate your {current_year} available TFSA room, I need your prior contributions, "
            "withdrawals, and residency timeline. CRA My Account is the authoritative source for your exact room."
        )
    elif re.search(r"contribute|deposit|transfer|add|invest", text):
        body = (
            "I can help prepare a contribution safely. Please provide the amount and confirm your available room first "
            "to avoid over-contribution penalties."
        )
    else:
        body = (
            "I can still help with TFSA policy basics while live services recover. "
            "Share whether you need eligibility, contribution room, timing rules, or contribution steps."
        )

    return (
        f"{body}\n\n"
        "Note: live model capacity is temporarily busy right now; please retry shortly for full dynamic lookup."
    )


def _log_route(state: "AgentState", node: str, decision: str, reason: str,
               data_selected: str, **extra) -> None:
    """Emit a ``routing_decision`` event: which branch the graph took and why.

    This is the agent's *plan* + data-source selection, otherwise invisible in the logs
    (the route_* functions are pure branching with no output). session_id/message_id are
    pulled from state so the event groups with the rest of the turn; trace_id/span_id are
    auto-attached by log_event from the current OTEL span.
    """
    log_event("routing_decision", agent="tfsa", node=node, decision=decision,
              reason=reason, data_selected=data_selected,
              session_id=state.get("session_id"), message_id=state.get("message_id"),
              user_id=state.get("user_id"), **extra)


def _invoke_llm(prompt: str, agent: str, use_thinking: bool = False):
    """Invoke the shared LLM, tagging the call with its prompt identity when supported.

    Custom LLMs (e.g. the watsonx wrapper) whose .invoke() doesn't accept a config kwarg
    fall back to a plain call so prompt tagging never breaks a provider.

    When ``use_thinking`` is set and a thinking-enabled instance exists (config.ENABLE_THINKING
    on Bedrock), the call is routed through it so its reasoning blocks are captured on
    llm_call_end; otherwise it transparently uses the standard temperature=0 ``llm``.

    Resilience: on top of the provider's transport retries, the call is retried up to
    config.LLM_INVOKE_ATTEMPTS times when it raises OR returns empty content (with jittered
    backoff). After exhausting attempts it RAISES — so callers' fallbacks produce a useful
    message instead of the user ever receiving a silent empty reply.
    """
    active_llm = thinking_llm if (use_thinking and thinking_llm is not None) else llm
    attempts = max(1, config.LLM_INVOKE_ATTEMPTS)
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            try:
                response = active_llm.invoke(prompt, config=_prompt_config(agent))
            except TypeError:
                response = active_llm.invoke(prompt)
            content = _extract_string_content(
                response.content if hasattr(response, "content") else response)
            if content:
                return response
            last_exc = ValueError("LLM returned empty content")
            logging.warning("Empty LLM response for %s (attempt %d/%d); retrying",
                            agent, attempt, attempts)
        except Exception as e:
            last_exc = e
            logging.warning("LLM call failed for %s (attempt %d/%d): %s",
                            agent, attempt, attempts, e)
        if attempt < attempts:
            time.sleep(min(2.0, 0.5 * attempt) + random.uniform(0, 0.3))
    raise last_exc if last_exc is not None else RuntimeError(f"LLM call failed for {agent}")


# Constants for TFSA limits file
TFSA_LIMITS_FILE = "tfsa_limits.json"

# Serializes the lazy refresh + file write of TFSA_LIMITS so concurrent requests don't each
# fire a duplicate search/LLM lookup or corrupt the file via interleaved writes.
_tfsa_limits_lock = threading.Lock()


def _load_or_update_tfsa_limits() -> dict:
    """
    Load TFSA limits from file or update them by searching for missing years.
    Returns a dictionary of year -> limit mappings.
    """
    current_year = datetime.datetime.now().year
    limits = {}

    # Base historical limits
    base_limits = {
        2009: 5000, 2010: 5000, 2011: 5000, 2012: 5000,
        2013: 5500, 2014: 5500, 2015: 10000, 2016: 5500,
        2017: 5500, 2018: 5500, 2019: 6000, 2020: 6000,
        2021: 6000, 2022: 6000, 2023: 6500, 2024: 7000
    }

    # Load from the configured data source (S3 if DATA_S3_BUCKET is set, else the local
    # tfsa_limits.json file). Falls back to base_limits if neither yields data.
    limits = data_sources.load_tfsa_limits()
    if limits:
        logging.info("Loaded TFSA limits from data source (%d years)", len(limits))
    else:
        limits = base_limits.copy()

    # Check if we have limits up to current year
    missing_years = []
    for year in range(2009, current_year + 1):
        if year not in limits:
            missing_years.append(year)

    # If we're missing current or future years, search for them
    if missing_years:
        logging.info(f"Missing TFSA limits for years: {missing_years}. Searching for updates...")
        updated_limits = _search_for_missing_limits(missing_years, limits)
        if updated_limits:
            limits.update(updated_limits)
            # Save updated limits to file atomically (write temp + os.replace) so a concurrent
            # or interrupted write can never leave a half-written / corrupt JSON file behind.
            try:
                serialized = {str(year): limit for year, limit in limits.items()}
                target_dir = os.path.dirname(os.path.abspath(TFSA_LIMITS_FILE))
                fd, tmp_path = tempfile.mkstemp(dir=target_dir, suffix=".tmp")
                with os.fdopen(fd, 'w') as f:
                    json.dump(serialized, f, indent=2)
                os.replace(tmp_path, TFSA_LIMITS_FILE)
                logging.info(f"Saved updated TFSA limits to {TFSA_LIMITS_FILE}")
            except Exception as e:
                logging.warning(f"Failed to save TFSA limits to file: {e}")

    return limits


def _search_for_missing_limits(missing_years: list, current_limits: dict) -> dict:
    """
    Search for missing TFSA limits using web search.
    Returns a dictionary of year -> limit mappings for the missing years.
    """
    try:
        # Create a search query for the missing years
        years_str = ", ".join(map(str, missing_years))
        search_query = f"Canada TFSA contribution limits {years_str}"

        # Resilient web search: Tavily with a DuckDuckGo fallback (handles Tavily's
        # non-raising error payloads too — see _run_policy_search).
        search_results = _run_policy_search(search_query)

        # Use LLM to extract the TFSA limits from search results
        prompt = f"""
        You are a financial policy expert. I need to find the TFSA (Tax-Free Savings Account) 
        annual contribution limits for the following years in Canada: {years_str}.

        Here are the search results:
        {json.dumps(search_results, indent=2, default=str)}

        Known historical limits:
        {json.dumps(current_limits, indent=2)}

        Please extract the official TFSA contribution limits for the missing years.
        Provide the information in JSON format with year as key and limit as value:
        {{
            "2025": 7000,
            "2026": 7500
        }}

        Only include the years you are confident about. If you cannot find information for a year,
        do not include it in the response. Respond with ONLY the JSON object.
        """

        # Use the resilient LLM wrapper (retries + empty-guard); the outer try/except below
        # degrades to base limits if it ultimately fails.
        response = _invoke_llm(prompt, "limits_search")
        response_content = _extract_string_content(response.content) if hasattr(response,
                                                                                'content') else _extract_string_content(
            response)

        # Parse the JSON response
        result = _get_json_from_str(response_content, {})

        # Validate that the result contains only numeric years and limits
        validated_result = {}
        for year, limit in result.items():
            try:
                year_int = int(year)
                limit_float = float(limit)
                if year_int in missing_years:  # Only include requested years
                    validated_result[year_int] = int(limit_float)
            except (ValueError, TypeError):
                logging.warning(f"Invalid year or limit in search result: {year} -> {limit}")
                continue

        logging.info(f"Found TFSA limits for years: {list(validated_result.keys())}")
        return validated_result

    except Exception as e:
        logging.error(f"Error searching for missing TFSA limits: {e}")
        return {}


# NOTE: TFSA_LIMITS is initialized further below, AFTER the search tools
# (search_cra_tfsa_policy / search_cra_tfsa_policy_duck_duck_go) are defined, since
# _load_or_update_tfsa_limits() may call them when current-year limits are missing.


# ======================
# 0. Help functions
# ======================
def _get_json_from_str(json_str: str, fallback_json: dict) -> dict:
    """Convert json string to dict"""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError) as e:
        # Recovered below via regex fallbacks, so this is a WARNING, not an ERROR. Log the
        # raw model output (truncated) — it's the only way to diagnose LLM format drift /
        # prompt-injection artifacts downstream; the generic exception message alone is useless.
        logging.warning("JSON parse miss (recovering): %s | raw=%r", e, str(json_str)[:1000])
        try:
            # First, try to extract JSON from Markdown code block
            code_block_match = re.search(r'```(?:json)?\s*({.*?})\s*```', json_str, re.DOTALL)
            if code_block_match:
                code_block_match_json_str = code_block_match.group(1)
            else:
                # Fallback: find first JSON object in the response
                json_match = re.search(r'\{.*?}', json_str, re.DOTALL)
                if json_match:
                    code_block_match_json_str = json_match.group()
                else:
                    raise ValueError("No JSON object found in response")

            # Clean invalid control characters
            code_block_match_json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', code_block_match_json_str)

            # Replace problematic newlines in string values
            code_block_match_json_str = re.sub(r':\s*"([^"]*?)"',
                                               lambda m: ': "' + m.group(1).replace('\n', '\\n') + '"',
                                               code_block_match_json_str)

            # Parse the cleaned JSON
            return json.loads(code_block_match_json_str)
        except (json.JSONDecodeError, ValueError) as e:
            logging.error(f"JSON parsing failed 2nd try: {str(e)}")
            # Try to extract the first valid JSON object
            try:
                start_idx = json_str.find('{')
                end_idx = json_str.rfind('}')
                if start_idx != -1 and end_idx != -1:
                    json_str = json_str[start_idx:end_idx + 1]
                    json_str = re.sub(r'[\x00-\x1f\x7f-\x9f]', ' ', json_str)
                    return json.loads(json_str)
                else:
                    return fallback_json
            except Exception as final_e:
                logging.error(f"JSON parsing failed 3rd try: {str(final_e)}")
                return fallback_json


# ======================
# 1. State Definition
# ======================
class AgentState(TypedDict):
    user_input: str
    user_id: str
    user_profile: Optional[dict]
    search_results: Optional[list]
    contribution_room: Optional[float]
    current_tfsa_limit: Optional[float]
    contribution_amount: Optional[float]
    # Top-level intent from the supervisor router (policy|room|contribute), or None when the
    # rules-based regex router is used. Set by profile_agent, read by the route_* functions.
    intent: Optional[str]
    # Carried so routing_decision / node_error events emitted from inside the graph group
    # with the turn's other events by conversation + message (trace_id joins automatically).
    session_id: Optional[str]
    message_id: Optional[str]
    messages: Annotated[list[dict], operator.add]


# ======================
# 2. Tool Definitions
# ======================
@tool
def retrieve_user_profile(user_id: str) -> dict:
    """Retrieves user's profile from the configured data source (S3) by user_id.

    Falls back to a built-in mock profile when no S3 bucket is configured or the
    user's object is missing (see data_sources.load_user_profile).
    """
    # TODO: gate on an authenticated identity (the user_id is still caller-supplied);
    #       integrate with the bank's SSO / JWT before exposing real PII.
    return data_sources.load_user_profile(user_id)


@tool
def search_cra_tfsa_policy_duck_duck_go(query: str) -> str:
    """Searches Canada CRA website for current TFSA policies"""
    from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
    search = DuckDuckGoSearchAPIWrapper()
    # Real-time policy verification using DuckDuckGo search
    return search.run(f"site:canada.ca TFSA {datetime.datetime.now().year} {query}")


@tool
def search_cra_tfsa_policy(query: str) -> list:
    """Searches Canada CRA website for current TFSA policies using Tavily"""
    # pip install -U langchain-tavily
    from langchain_tavily import TavilySearch
    # search_depth/include_answer/include_raw_content must be set at instantiation;
    # langchain-tavily rejects them as per-invocation params.
    tavily = TavilySearch(
        api_key=config.TAVILY_API_KEY,
        max_results=3,
        search_depth="advanced",
        include_answer=True,
        include_raw_content=True,
    )
    # Real-time policy verification using Tavily search
    results = tavily.invoke({
        "query": f"site:canada.ca TFSA {datetime.datetime.now().year} {query}",
    })
    return results


def _run_policy_search(query: str):
    """Run the CRA policy web search with a resilient Tavily -> DuckDuckGo fallback.

    Tavily's wrapper does NOT raise on quota/auth problems — it returns an
    ``{"error": <Exception>}`` payload (e.g. "Error 432: usage limit"). A raw exception
    OR such an error payload is treated as a failure here so we fall back to DuckDuckGo
    instead of feeding an unusable (and non-JSON-serializable) result downstream.
    """
    try:
        results = search_cra_tfsa_policy.invoke(query)
        if isinstance(results, dict) and results.get("error"):
            raise RuntimeError(f"Tavily returned an error payload: {results['error']}")
        return results
    except Exception as e:
        logging.warning("Tavily search unavailable (%s); falling back to DuckDuckGo.", e)
        return search_cra_tfsa_policy_duck_duck_go.invoke(query)


# Load TFSA limits from the configured data source (or local file/S3) at startup.
# This prevents module import-time hangs caused by web searches or LLM calls.
TFSA_LIMITS = data_sources.load_tfsa_limits()
logging.info(f"TFSA Limits loaded: {TFSA_LIMITS}")


@tool
def execute_tfsa_contribution(user_id: str, amount: float) -> dict:
    """Executes TFSA contribution transaction from checking account"""
    # Mock implementation - replace with banking API
    if not user_id:
        return {
            "status": "failure",
            "reason": "User ID not provided"
        }
    profile = retrieve_user_profile.invoke(user_id)
    if amount > profile["checking_balance"]:
        return {"status": "failed", "reason": "Insufficient funds"}

    new_contributions = profile["current_year_contributions"] + amount
    return {
        "status": "success",
        "new_balance": profile["past_contributions"] + new_contributions,  # Base + contributions
        "new_contributions": new_contributions,
        "transaction_id": f"TFSA-{datetime.datetime.now().year}-{uuid.uuid4()}"
    }


# ======================
# 3. Agent Definitions
# ======================
def _has_real_user_id(state: AgentState) -> bool:
    """True only when a concrete identity was supplied ("unknown" is the no-id sentinel)."""
    uid = state.get("user_id")
    return bool(uid) and uid != "unknown"


def _load_profile_into_state(state: AgentState):
    """Load the user's profile on demand (only lanes that need it call this).

    Returns the profile dict (also cached on state['user_profile']) or None when no real
    identity was supplied. Emits a ``data_source`` audit event so the logs show whether the
    answer used real S3 data or the built-in mock fallback (otherwise the two are
    indistinguishable downstream).
    """
    if not _has_real_user_id(state):
        return None
    if state.get("user_profile"):
        return state["user_profile"]
    uid = state["user_id"]
    profile = retrieve_user_profile.invoke(uid)
    source = profile.pop("_source", "unknown") if isinstance(profile, dict) else "unknown"
    log_event("data_source", agent="tfsa", entity="profile", user_id=uid, source=source,
              session_id=state.get("session_id"), message_id=state.get("message_id"))
    state["user_profile"] = profile
    return profile


_VALID_INTENTS = {"policy", "room", "contribute", "advisory"}


def _classify_intent_supervisor(user_input: str) -> Optional[str]:
    """LLM router: classify the request into policy | room | contribute.

    Returns one of _VALID_INTENTS, or None if the LLM is unavailable / returns something
    unexpected (callers then fall back to the deterministic regex router).
    """
    prompt = f"""You route requests for a TFSA assistant. Classify the user's message into
exactly one intent.

User message: "{user_input}"

Intents:
- "policy": a single general TFSA rules/limits/penalties/eligibility/deadlines question.
- "room": the user wants their available contribution room / how much they can contribute.
- "contribute": the user wants to make/execute a contribution or transfer money now.
- "advisory": a compound, what-if, or advice question that needs combining several facts
  (e.g. "what's my room AND should I top up before year-end?", "what if I withdraw $5k?",
  "how much room will I have in 2 years?").

Respond with raw JSON ONLY — no markdown, no code fences, no extra text:
{{"reasoning": "one short sentence", "intent": "policy" | "room" | "contribute" | "advisory"}}"""
    try:
        response = _invoke_llm(prompt, "supervisor_router")
        content = _extract_string_content(
            response.content if hasattr(response, "content") else response)
        data = _get_json_from_str(content, {})
        intent = (data.get("intent") or "").strip().lower()
        return intent if intent in _VALID_INTENTS else None
    except Exception as e:
        logging.warning("Supervisor router failed (%s); falling back to rules router.", e)
        return None


@trace_node("profile_agent")
def profile_agent(state: AgentState):
    """Entry node. Does NOT fetch the profile (loaded on demand by the lanes that need it).

    When config.ROUTER_MODE == "supervisor", an LLM classifies the top-level intent here and
    stores it on state so the route_* functions can act on it (with a regex fallback). In
    "rules" mode this is a no-op and routing stays purely deterministic.
    """
    if config.ROUTER_MODE == "supervisor":
        intent = _classify_intent_supervisor(state["user_input"])
        if intent:
            log_event("agent_reasoning", agent="tfsa", node="supervisor_router",
                      reasoning=f"classified intent={intent}", intent=intent,
                      session_id=state.get("session_id"), message_id=state.get("message_id"),
                      user_id=state.get("user_id"))
            return {"intent": intent}
    return {}


@trace_node("document_agent")
def document_agent(state: AgentState):
    """Agent with knowledge of historical TFSA rules"""
    current_year = datetime.datetime.now().year
    # Handle case where user profile might be missing
    if state.get("user_profile"):
        user_info = f"User: {state['user_profile']['name']}, Age: {state['user_profile']['age']}"
    else:
        user_info = "User: General Inquiry"

    # Use the limits actually loaded from the data source (S3/local), which typically include
    # the current year — so the model can answer current-year questions WITHOUT a web search.
    # Only a year missing from this list should trigger needs_current_search.
    known = sorted(TFSA_LIMITS.items())
    limits_text = "\n".join(f"    - {year}: ${limit:,}" for year, limit in known) or "    (none loaded)"
    max_known_year = max(TFSA_LIMITS) if TFSA_LIMITS else current_year - 1

    prompt = f"""
    You are a TFSA (Tax-Free Savings Account) policy expert at a Canadian bank.
    Current year: {current_year}
    {user_info}
    User question: {state['user_input']}

    Authoritative annual TFSA contribution limits (from the bank's data source):
{limits_text}

    Known rules:
    - Unused contribution room carries forward indefinitely.
    - Withdrawals are added back to contribution room the FOLLOWING calendar year, not the same year.
    - Over-contribution penalty: 1% per month on the excess amount.
    - You must be 18+ and a Canadian resident to contribute.

    Respond with JSON ONLY containing:
    {{
      "reasoning": "1-2 sentences: why you chose this answer and whether you need live data. Audit-only.",
      "policy_summary": "Your answer to the user's question, using only the facts above.",
      "needs_current_search": true/false
    }}

    Instructions:
    - Answer from the limits and rules above, which are authoritative. Never invent a figure.
    - The list above already includes the current year ({current_year}) when available, so a
      current-year limit question is answerable directly.
    - Set "needs_current_search" to true ONLY if answering needs a year's limit that is NOT in
      the list above (i.e. a year after {max_known_year}). Otherwise set it to false.
    - For historical/current limit questions, list the relevant year ranges with their amounts:
        TFSA Annual Contribution Limits:
        [YEAR RANGE]: $AMOUNT
      and note that cumulative room for someone eligible since 2009 is the sum of all annual
      limits up to and including the year in question.
    """

    # Use unified LLM interface. Guard the call: a Bedrock ThrottlingException here (this is the
    # first LLM node, hit on every policy question) would otherwise propagate to the workflow
    # loop, leave no assistant message, and surface to the user as "No response generated".
    try:
        if hasattr(llm, 'invoke'):
            response = _invoke_llm(prompt, "document_agent", use_thinking=True)
            response_content = _extract_string_content(response.content) if hasattr(response,
                                                                                    'content') else _extract_string_content(
                response)
        else:
            response_content = _invoke_llm(prompt, "document_agent", use_thinking=True)
    except Exception as e:
        log_event("node_error", agent="tfsa", node="document_agent",
                  error=str(e), error_type=type(e).__name__, stage="llm_call",
                  session_id=state.get("session_id"), message_id=state.get("message_id"),
                  user_id=state.get("user_id"))
        return {"messages": [{
            "role": "assistant",
            "content": ("I'm experiencing high demand right now and couldn't complete that "
                        "request. Please try again in a few moments."),
        }]}

    data = _get_json_from_str(response_content, {
        "reasoning": "",
        "policy_summary": response_content,
        "needs_current_search": True,
        "_parse_failed": True
    })
    if data.pop("_parse_failed", False):
        log_event("node_error", agent="tfsa", node="document_agent",
                  error="LLM output was not valid JSON; used fallback", error_type="json_parse",
                  session_id=state.get("session_id"), message_id=state.get("message_id"),
                  user_id=state.get("user_id"))
    # Audit-only: capture the model's rationale (kept out of the user-facing content).
    log_event("agent_reasoning", agent="tfsa", node="document_agent",
              reasoning=data.get("reasoning", ""),
              needs_search=data.get("needs_current_search"),
              session_id=state.get("session_id"), message_id=state.get("message_id"),
              user_id=state.get("user_id"))
    return {
        "messages": [{
            "role": "document_agent",
            "content": data["policy_summary"],
            "needs_search": data["needs_current_search"]
        }]
    }


@trace_node("search_agent")
def search_agent(state: AgentState):
    """Agent that searches for current TFSA policies using Tavily"""
    try:
        # Use original user query instead of fixed term
        query = f"{state['user_input']}"

        # Resilient web search: Tavily with a DuckDuckGo fallback (also handles Tavily's
        # non-raising error payloads — see _run_policy_search).
        results = _run_policy_search(query)

        # Search results: {results}
        # Extract key information. Process results with LLM
        prompt = f"""
        You are a helpful TFSA specialist at a Canadian bank. The CRA search results below are
        your source of truth for current-year data — ground every figure in them and never guess.
        Current year: {datetime.datetime.now().year}

        Search results:
        {json.dumps(results, indent=2, default=str)}

        The user asked: "{state['user_input']}"

        Return a JSON object with exactly these fields:
        {{
          "reasoning": "1-2 sentences: how the search results ground your answer. Audit-only.",
          "answer": "User-friendly response to the query, grounded in the search results.",
          "current_limit": "The {datetime.datetime.now().year} annual TFSA contribution limit as a dollar amount, e.g. \\"$7,000\\". Use \\"unknown\\" if the results do not state it.",
          "penalty_info": "One-sentence summary of over-contribution penalties.",
          "withdrawal_rules": "One-sentence summary of withdrawal / re-contribution rules."
        }}


        Instructions for the "answer" field:
        - If the search results are missing or conflict on a figure, say so rather than inventing one.
        - For contribution requests, respond conversationally and ask what you need to help:
            "I'd be happy to help you contribute to your TFSA!
            To give you the best guidance, could you tell me:
            * Do you already have a TFSA account?
            * Are you making a one-time or recurring contribution?
            * Do you know your available contribution room?

            In the meantime, here are the key things to know:
            [key contribution facts from the search results]"
        - For policy questions, give the relevant limits and rules clearly.
        - For future years (beyond {datetime.datetime.now().year}), note that limits are projections
          subject to inflation adjustment.
        - Use simple language and bullet points; be professional but conversational.

        Important: Respond with ONLY the JSON object — no extra text, explanations, or markdown
        fences. It must be valid JSON that can be parsed directly.
        """
        response = _invoke_llm(prompt, "search_agent", use_thinking=True)
        # Access the content attribute of the response
        response_content = _extract_string_content(response.content) if hasattr(response,
                                                                                'content') else _extract_string_content(
            response)

        # Try to parse the JSON response
        policy_data = _get_json_from_str(response_content,
                                         {"reasoning": "", "answer": response_content,
                                          "error": "Could not parse policy data"})
        if policy_data.get("error") == "Could not parse policy data":
            log_event("node_error", agent="tfsa", node="search_agent",
                      error="LLM output was not valid JSON; used fallback", error_type="json_parse",
                      session_id=state.get("session_id"), message_id=state.get("message_id"),
                      user_id=state.get("user_id"))
        # Audit-only: capture the model's rationale (kept out of the user-facing content).
        log_event("agent_reasoning", agent="tfsa", node="search_agent",
                  reasoning=policy_data.get("reasoning", ""),
                  session_id=state.get("session_id"), message_id=state.get("message_id"),
                  user_id=state.get("user_id"))

        # Extract and store the current year's limit directly in the state
        current_limit = None
        try:
            limit_str = str(policy_data.get("current_limit", ""))
            match = re.search(r'\$?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)', limit_str)
            if match:
                current_limit = float(match.group(1).replace(',', ''))
        except (ValueError, TypeError):
            logging.warning("Could not parse current_limit from search_agent response.")

        return {
            "search_results": results,
            "current_tfsa_limit": current_limit,
            "messages": [{
                "role": "search_agent",
                "content": f"{policy_data}",
                "policy_data": policy_data
            }]
        }
    except Exception as e:
        log_event("node_error", agent="tfsa", node="search_agent",
                  error=str(e), error_type=type(e).__name__, stage="search",
                  session_id=state.get("session_id"), message_id=state.get("message_id"),
                  user_id=state.get("user_id"))
        return {
            "messages": [{
                "role": "search_agent",
                "content": f"⚠️ Search failed: {str(e)}"
            }]
        }


def _current_year_limit(override: Optional[float] = None) -> float:
    """Current-year TFSA limit. Refreshes TFSA_LIMITS once (double-checked lock) if the year is
    missing, so concurrent callers don't each re-search. `override` (e.g. a value already parsed
    from a live search) takes precedence when truthy."""
    current_year = datetime.datetime.now().year
    global TFSA_LIMITS
    if current_year not in TFSA_LIMITS:
        with _tfsa_limits_lock:
            if current_year not in TFSA_LIMITS:  # re-check after acquiring the lock
                TFSA_LIMITS = _load_or_update_tfsa_limits()
    return override or TFSA_LIMITS.get(current_year, 6000)


def _compute_contribution_room(profile: dict, current_limit: float, current_year: int) -> dict:
    """Pure TFSA room math shared by calculation_agent and the get_tfsa_room tool.

    Returns {available_room, total_room, used_room}. Accumulated room is the sum of annual
    limits from the user's first eligible year through the current year; available room
    subtracts contributions to date and adds back prior-year withdrawals.
    """
    birth_year = current_year - profile["age"]
    first_year = max(profile["first_tfsa_year"], birth_year + 18)
    total_room = sum(TFSA_LIMITS.get(y, 0) for y in range(first_year, current_year)) + current_limit
    used_room = profile["past_contributions"] + profile["current_year_contributions"]
    available_room = total_room - used_room + profile["withdrawals_last_year"]
    return {"available_room": available_room, "total_room": total_room, "used_room": used_room}


# ---- Read-only tools for the advisor (ReAct) node. The LLM selects among these; money
# movement (execute_tfsa_contribution) is deliberately NOT exposed here. ----
@tool
def get_tfsa_room(user_id: str) -> dict:
    """Get a user's available TFSA contribution room for the current year, with the breakdown
    (total accumulated room, contributions to date, withdrawals added back)."""
    profile = data_sources.load_user_profile(user_id)
    current_year = datetime.datetime.now().year
    room = _compute_contribution_room(profile, _current_year_limit(), current_year)
    return {"user_id": user_id, "year": current_year, **room}


@tool
def get_transaction_history(user_id: str) -> list:
    """Get a user's past TFSA contribution / withdrawal transactions (most useful for
    explaining how their current contribution room was reached)."""
    return data_sources.load_user_transactions(user_id)


@tool
def lookup_tfsa_limit(year: int) -> dict:
    """Look up the annual TFSA contribution limit for a single year."""
    try:
        y = int(year)
    except (ValueError, TypeError):
        return {"year": year, "limit": None, "error": "invalid year"}
    _current_year_limit()  # ensure the current year is loaded into TFSA_LIMITS
    return {"year": y, "limit": TFSA_LIMITS.get(y)}


@tool
def simulate_withdrawal(user_id: str, amount: float) -> dict:
    """Estimate the effect of withdrawing `amount` from a TFSA now. A withdrawal does NOT free
    up contribution room in the same year; the amount is added back on January 1 of next year."""
    current_year = datetime.datetime.now().year
    profile = data_sources.load_user_profile(user_id)
    room = _compute_contribution_room(profile, _current_year_limit(), current_year)
    return {
        "user_id": user_id,
        "withdrawal_amount": amount,
        "room_this_year_after_withdrawal": room["available_room"],  # unchanged this year
        "room_added_back_on": f"{current_year + 1}-01-01",
        "room_added_back_amount": amount,
        "note": "Withdrawals are re-added to contribution room on Jan 1 of the FOLLOWING year.",
    }


@tool
def project_future_room(user_id: str, years: int = 1) -> dict:
    """Project a user's available TFSA room `years` years into the future, assuming future annual
    limits equal the most recent known limit (a projection, not a guarantee)."""
    current_year = datetime.datetime.now().year
    profile = data_sources.load_user_profile(user_id)
    current_limit = _current_year_limit()
    base = _compute_contribution_room(profile, current_limit, current_year)["available_room"]
    try:
        n = max(0, int(years))
    except (ValueError, TypeError):
        n = 1
    return {
        "user_id": user_id,
        "current_year": current_year,
        "available_room_now": base,
        "projected_years": n,
        "projected_available_room": base + current_limit * n,
        "assumption": f"future annual limits assumed = latest known limit (${current_limit:,.0f})",
    }


@trace_node("calculation_agent")
def calculation_agent(state: AgentState):
    """Calculates contribution room based on profile and policies"""
    # Require a concrete identity ("unknown" means none was supplied).
    if not _has_real_user_id(state):
        return {
            "messages": [{
                "role": "assistant",
                "content": ("I need your user ID to calculate your contribution room. "
                            "Please provide it (e.g. 'my user id is user_123').")
            }]
        }
    # Load the profile on demand (emits a data_source event: s3 vs mock).
    profile = _load_profile_into_state(state)
    current_year = datetime.datetime.now().year
    current_limit = _current_year_limit(state.get("current_tfsa_limit"))

    if profile:
        room = _compute_contribution_room(profile, current_limit, current_year)
        available_room = room["available_room"]
        response = (
            f"Based on your profile, your available TFSA contribution room for {current_year} is ${available_room:.2f}.\n"
            f"* Total accumulated room: ${room['total_room']:.2f}\n"
            f"* Contributions to date: ${room['used_room']:.2f}\n"
            f"* Withdrawals added back: ${profile['withdrawals_last_year']:.2f}"
        )
    else:
        available_room = 0
        response = f"Available contribution room: ${available_room:.2f}"

    return {
        "contribution_room": available_room,
        "messages": [{
            "role": "assistant",  # Changed to assistant role
            "content": response
        }]
    }


@trace_node("transaction_agent")
def transaction_agent(state: AgentState):
    """Handles transaction execution"""
    # Require a concrete identity ("unknown" means none was supplied).
    if not _has_real_user_id(state):
        return {
            "messages": [{
                "role": "assistant",
                "content": ("I need your user ID to make a contribution. "
                            "Please provide it (e.g. 'my user id is user_123').")
            }]
        }
    # TODO: Encrypt PII data using AES-256
    # TODO: Add transaction confirmation step
    # TODO: Implement fraud detection hooks
    # Only execute for concrete user intent, not hypothetical/policy/security probes.
    if not _is_transaction_request(state["user_input"]):
        return {
            "messages": [{
                "role": "assistant",
                "content": "I can explain TFSA transfer rules and safeguards, but I won't execute a transaction unless you explicitly request one with an amount."
            }]
        }

    # Extract amount from user input
    amount = _extract_amount_from_text(state["user_input"])

    if amount <= 0:
        return {
            "messages": [{
                "role": "assistant",
                "content": "Please specify a valid contribution amount (e.g., '$500')"
            }]
        }

    # Validate against contribution room
    if amount > state["contribution_room"]:
        return {
            "messages": [{
                "role": "assistant",
                "content": f"⚠️ Amount exceeds contribution room by ${amount - state['contribution_room']:.2f}"
            }]
        }

    # Execute transaction
    result = execute_tfsa_contribution.invoke({"user_id": state["user_id"], "amount": amount})

    if result["status"] == "success":
        new_room = state["contribution_room"] - amount
        return {
            "contribution_amount": amount,
            "messages": [{
                "role": "assistant",
                "content": (
                    f"✅ Success! Transferred ${amount:.2f} to your TFSA\n"
                    f"* New TFSA balance: ${result['new_balance']:.2f}\n"
                    f"* Remaining contribution room: ${new_room:.2f}\n"
                    f"* Transaction ID: {result['transaction_id']}"
                )
            }]
        }
    else:
        return {
            "messages": [{
                "role": "assistant",
                "content": f"❌ Transaction failed: {result['reason']}"
            }]
        }


_ADVISOR_TOOLS = [get_tfsa_room, get_transaction_history, lookup_tfsa_limit,
                  simulate_withdrawal, project_future_room]
_advisor_react_agent = None


def _get_advisor_agent():
    """Lazily build (once) the ReAct agent that lets the LLM select read-only tools."""
    global _advisor_react_agent
    if _advisor_react_agent is None:
        from langgraph.prebuilt import create_react_agent
        _advisor_react_agent = create_react_agent(llm, _ADVISOR_TOOLS)
    return _advisor_react_agent


@trace_node("advisor_agent")
def advisor_agent(state: AgentState, config=None):
    """LLM tool-calling advisor for advisory / compound / what-if questions.

    The model decides which read-only tools to call (room, limits, history, projections), so the
    audit stream captures a real plan trace: llm_call -> tool_call(chosen) -> llm_call -> answer.
    `config` is forwarded so the parent graph's AuditCallbackHandler propagates into the
    sub-agent's internal LLM + tool calls. Money movement is NOT available here.
    """
    uid = state.get("user_id")
    if _has_real_user_id(state):
        identity = f"The user's user_id is {uid}; pass it to user-specific tools."
    else:
        identity = ("No user_id was provided; if a tool needs one, ask the user for it "
                    "(e.g. 'my user id is user_123') instead of guessing.")
    system = (
        "You are a TFSA advisor at a Canadian bank. Use the available tools to look up the "
        "user's contribution room, annual limits, and transaction history before answering — "
        "never invent figures. You cannot move money; if the user wants to contribute, explain "
        "the steps and ask them to confirm the amount. Be concise and conversational. " + identity
    )
    msgs = [{"role": "system", "content": system},
            {"role": "user", "content": state["user_input"]}]
    try:
        result = _get_advisor_agent().invoke({"messages": msgs}, config=config)
        final = result["messages"][-1].content if result.get("messages") else ""
        content = _extract_string_content(final)
        # Nova-style models emit chain-of-thought as literal <thinking> text; keep it in the
        # audit log only and strip it from the user-facing reply.
        content, thinking = _split_thinking(content)
        if thinking:
            log_event("agent_reasoning", agent="tfsa", node="advisor_agent",
                      reasoning=thinking, session_id=state.get("session_id"),
                      message_id=state.get("message_id"), user_id=uid)
    except Exception as e:
        log_event("node_error", agent="tfsa", node="advisor_agent", error=str(e),
                  error_type=type(e).__name__, stage="advisor",
                  session_id=state.get("session_id"), message_id=state.get("message_id"),
                  user_id=uid)
        content = ""
    if not content.strip():
        content = "I couldn't complete that request right now. Please try again in a moment."
    return {"messages": [{"role": "assistant", "content": content}]}


@trace_node("response_agent")
def response_agent(state: AgentState):
    """Formats final response using LLM to create coherent human-readable answer"""
    # Collect all assistant messages
    assistant_messages = [msg["content"] for msg in state["messages"] if msg.get("role") == "assistant"]
    current_year = datetime.datetime.now().year

    # Collect policy information if available
    policy_info = ""
    for msg in reversed(state["messages"]):
        if msg.get("role") == "search_agent" and "policy_data" in msg:
            policy = msg["policy_data"]
            policy_info = (
                f"Answer to user input:{policy.get('answer', 'N/A')}\n"
                f"Current TFSA Policy Information:\n"
                f"* Contribution Limit: {policy.get('current_limit', 'N/A')}\n"
                f"* Penalties: {policy.get('penalty_info', 'N/A')}\n"
                f"* Withdrawal Rules: {policy.get('withdrawal_rules', 'N/A')}"
            )
            break

    # Collect document agent's response properly
    document_response = "N/A"
    for msg in state["messages"]:
        if msg.get("role") == "document_agent":
            document_response = msg.get("content", "")
            break

    # Get contribution room if available
    contribution_room = state.get("contribution_room", 0)
    contribution_amount = state.get("contribution_amount", 0)

    # Prepare context for LLM
    user_input = state["user_input"]
    context = {
        "user_question": user_input,
        "assistant_responses": "\n".join(assistant_messages),
        "policy_information": policy_info,
        "document_response": document_response,
        "contribution_room": f"${contribution_room:.2f}" if contribution_room else "Not calculated",
        "contribution_amount": f"${contribution_amount:.2f}" if contribution_amount else "None",
        "current_year": current_year
    }

    # Create prompt for final response generation
    prompt = f"""
    You are a certified TFSA specialist at a Canadian bank. Write a single, coherent, human-readable
    reply to the user using ONLY the information provided below. Never invent figures — if a value is
    not present in the information below, do not state it.

    User question:
    {context['user_question']}

    Information gathered by the assistant:
    {context['assistant_responses']}

    Additional context:
    - Current year: {current_year}
    - Policy information: {context['policy_information']}
    - Available contribution room: {context['contribution_room']}
    - Contribution amount: {context['contribution_amount']}

    Response guidelines:
    1. Address the user's question directly first.
    2. Organize information logically: policy → calculations → actions taken.
    3. Use simple language and bullet points for readability.
    4. Include these elements when they appear in the information above:
       - The {current_year} contribution limit
       - Over-contribution penalty risk
       - Withdrawal re-contribution rules
       - Transaction ID, if a contribution was made
    5. End with a helpful follow-up question or next-step suggestion.
    6. If the user asked about historical limits, list every year range and amount that appears
       in the information above. Format as:
         TFSA Annual Contribution Limits:
         [YEAR RANGE]: $AMOUNT

    Style:
    - Professional but conversational tone.
    - Keep the response under 300 words.
    - Do NOT mention that you are synthesizing information or reference these instructions.

    Output format (follow exactly):
    - First, one line of audit-only reasoning starting with "REASONING:" (1-2 sentences on how
      you synthesized the reply from the information above).
    - Then a line containing exactly: ###ANSWER###
    - Then the user-facing reply. Never repeat the word REASONING or the ###ANSWER### separator
      inside the reply itself.
    """

    # Check if we can bypass the LLM call when we already have a complete answer from document_agent
    # and no search/calculation/transaction was performed.
    document_needs_search = False
    for msg in state["messages"]:
        if msg.get("role") == "document_agent":
            document_needs_search = msg.get("needs_search", False)
            break

    has_calculation = "contribution_room" in state and state["contribution_room"] is not None
    contribution_amount = state.get("contribution_amount")
    has_transaction = "transaction_id" in state or (
            isinstance(contribution_amount, (int, float)) and contribution_amount > 0)

    if document_response != "N/A" and not document_needs_search and not has_calculation and not has_transaction:
        logging.info(
            "Bypassing response_agent LLM call: document_agent provided complete answer without search/calculations.")
        final_content = document_response
        # Cache the result (scoped by user so personalized answers aren't shared)
        cache_hash = _response_cache_key(user_input, state.get("user_id"))
        cache.cache(cache_hash, final_content, metadata={"user_input": user_input})
    else:
        # Generate final response using LLM
        try:
            if hasattr(llm, 'invoke'):
                response = _invoke_llm(prompt, "response_agent", use_thinking=True)
                final_content = _extract_string_content(response.content) if hasattr(response,
                                                                                     'content') else _extract_string_content(
                    response)
            else:
                final_content = _invoke_llm(prompt, "response_agent", use_thinking=True)

            # Split off the audit-only reasoning prefix so it never reaches the user. If the model
            # omitted the separator we treat the whole output as the reply (reasoning stays empty).
            reasoning_text = ""
            if "###ANSWER###" in final_content:
                reasoning_part, final_content = final_content.split("###ANSWER###", 1)
                reasoning_text = re.sub(r'^\s*REASONING:\s*', '', reasoning_part.strip()).strip()
                final_content = final_content.strip()
            # Strip any literal <thinking> the model emitted (Nova-style); log it, don't show it.
            final_content, thinking_text = _split_thinking(final_content)
            if thinking_text:
                reasoning_text = f"{reasoning_text}\n{thinking_text}".strip()
            log_event("agent_reasoning", agent="tfsa", node="response_agent",
                      reasoning=reasoning_text,
                      session_id=state.get("session_id"), message_id=state.get("message_id"),
                      user_id=state.get("user_id"))

            # Patch final_content to make it more readable
            final_content = final_content.replace("• ", "* ")
            final_content = final_content.replace("    ", "")
            # Convert to lowercase for case-insensitive search
            targets = ["response:", "answer:"]

            for target in targets:
                lower_content = final_content.lower()
                # Find the first occurrence index
                index = lower_content.find(target)

                if index != -1:
                    # Extract content after "response:" including its original case
                    result = final_content[index + len(target):]
                    # Trim leading/trailing whitespace
                    final_content = result.strip()
                else:
                    final_content = final_content.strip()

            if len(final_content) > 0:
                # Cache the response, scoped by user so it isn't reused across users.
                cache_hash = _response_cache_key(user_input, state.get("user_id"))
                cache.cache(cache_hash, final_content, metadata={"user_input": user_input})
        except Exception as e:
            logging.error(f"Response generation failed: {str(e)}")
            log_event("node_error", agent="tfsa", node="response_agent",
                      error=str(e), error_type=type(e).__name__, stage="response_synthesis",
                      session_id=state.get("session_id"), message_id=state.get("message_id"),
                      user_id=state.get("user_id"))
            # Fallback to intermediate messages if the final LLM call failed (e.g. throttled)
            fallback_parts = []
            if document_response and document_response != "N/A":
                fallback_parts.append(document_response)
            for msg in state["messages"]:
                role = msg.get("role")
                if role in ["document_agent", "search_agent", "calculation_agent", "transaction_agent"] and msg.get(
                        "content"):
                    if msg["content"] not in fallback_parts:
                        fallback_parts.append(msg["content"])
            final_content = "\n\n".join(
                fallback_parts) if fallback_parts else "I encountered an error generating the response. Please try again."

    return {
        "messages": [{
            "role": "assistant",
            "content": final_content
        }]
    }


# ======================
# 4. Graph Construction
# ======================
def create_workflow() -> CompiledStateGraph:
    workflow = StateGraph(AgentState)

    # Define nodes
    workflow.add_node("profile_agent", profile_agent)
    workflow.add_node("document_agent", document_agent)
    workflow.add_node("search_agent", search_agent)
    workflow.add_node("calculation_agent", calculation_agent)
    workflow.add_node("transaction_agent", transaction_agent)
    workflow.add_node("advisor_agent", advisor_agent)
    workflow.add_node("response_agent", response_agent)

    # Define edges
    workflow.set_entry_point("profile_agent")

    # Conditional edge after profile agent
    def route_after_profile(state: AgentState):
        """Decide next step after profile_agent"""
        # Supervisor router: act on the LLM-classified intent when present.
        intent = state.get("intent")
        if intent in ("room", "contribute"):
            _log_route(state, "profile_agent", "calculation_agent",
                       f"supervisor intent={intent}", "user_profile+room_calc")
            return "calculation_agent"
        if intent == "policy":
            _log_route(state, "profile_agent", "document_agent",
                       "supervisor intent=policy", "static_policy_kb")
            return "document_agent"
        if intent == "advisory":
            _log_route(state, "profile_agent", "advisor_agent",
                       "supervisor intent=advisory — LLM tool-calling advisor", "advisor_tools")
            return "advisor_agent"

        # Fallback: deterministic regex router (rules mode, or supervisor returned nothing).
        user_input = state["user_input"].lower()

        # Handle calculation requests (contribution room)
        if (re.search(r"contribution room|how much can i contribute|room available|limit available", user_input) or
                "how much" in user_input and ("contribute" in user_input or "room" in user_input)):
            _log_route(state, "profile_agent", "calculation_agent",
                       "matched contribution-room intent", "user_profile+room_calc")
            return "calculation_agent"

        # Handle transaction requests
        if _is_transaction_request(user_input):
            _log_route(state, "profile_agent", "calculation_agent",
                       "matched transaction intent", "user_profile+room_calc")
            return "calculation_agent"  # Need room calculation first

        _log_route(state, "profile_agent", "document_agent",
                   "no calc/txn keyword — policy question", "static_policy_kb")
        return "document_agent"

    workflow.add_conditional_edges(
        "profile_agent",
        route_after_profile,
        {
            "document_agent": "document_agent",
            "calculation_agent": "calculation_agent",
            "advisor_agent": "advisor_agent"
        }
    )

    # Conditional edge after calculation agent
    def route_after_calculation(state: AgentState):
        """Decide next step after calculation"""
        # Supervisor router: only an explicit "contribute" intent proceeds to execution.
        intent = state.get("intent")
        if intent in ("room", "policy"):
            _log_route(state, "calculation_agent", "response_agent",
                       f"supervisor intent={intent} — no transaction", "user_profile+room_calc")
            return "response_agent"
        if intent == "contribute":
            _log_route(state, "calculation_agent", "transaction_agent",
                       "supervisor intent=contribute", "user_profile+room_calc")
            return "transaction_agent"

        # Fallback: deterministic regex router.
        user_input = state["user_input"].lower()

        # Handle transaction requests
        if _is_transaction_request(user_input):
            _log_route(state, "calculation_agent", "transaction_agent",
                       "matched execute-transaction intent", "user_profile+room_calc")
            return "transaction_agent"
        # Informational room query: still go through response_agent for a consistent,
        # polished final answer (same as the policy lane) rather than returning the raw node text.
        _log_route(state, "calculation_agent", "response_agent",
                   "informational room query — no transaction", "user_profile+room_calc")
        return "response_agent"

    workflow.add_conditional_edges(
        "calculation_agent",
        route_after_calculation,
        {
            "transaction_agent": "transaction_agent",
            "response_agent": "response_agent"
        }
    )

    # Conditional edge after document agent
    def route_after_document(state: AgentState):
        """Decide next step after document_agent"""
        # Always search if needed
        if any(msg.get("needs_search", False) for msg in state["messages"]):
            _log_route(state, "document_agent", "search_agent",
                       "needs_current_search=true (post-2024 data not in KB)", "live_cra_search")
            return "search_agent"

        _log_route(state, "document_agent", "response_agent",
                   "answerable from static KB", "static_policy_kb")
        return "response_agent"

    workflow.add_conditional_edges(
        "document_agent",
        route_after_document,
        {
            "search_agent": "search_agent",
            "response_agent": "response_agent"
        }
    )

    # Define terminal edges for the graph. The policy/room/transaction lanes converge on
    # response_agent for consistent final formatting. The advisor_agent is itself an LLM agent
    # that already produces a complete, conversational answer, so it goes straight to END
    # (avoids a redundant response_agent LLM pass).
    workflow.add_edge("transaction_agent", "response_agent")
    workflow.add_edge("search_agent", "response_agent")
    workflow.add_edge("advisor_agent", END)
    workflow.add_edge("response_agent", END)

    # Compile the graph
    compiled_state_graph = workflow.compile()

    if _should_write_graph_image():
        try:
            png_graph = compiled_state_graph.get_graph().draw_mermaid_png()
            with open("tfsa_graph.png", "wb") as f:
                f.write(png_graph)
            logging.info(f"Graph saved as 'tfsa_graph.png' in {os.getcwd()}")
        except Exception as e:
            logging.warning(f"Could not draw graph: {e}. Please install graphviz and its dependencies.")

    return compiled_state_graph


graph_app = create_workflow()


# ======================
# 5. Execution Function
# ======================
def extract_user_id(input_str: str) -> str:
    """Extracts user ID from input string"""
    # Look for patterns like "user ID is XYZ", "my ID is XYZ", etc.
    patterns = [
        r"my\s+user\s*id\s+is\s+(\w+)",
        r"user\s*id:\s*(\w+)",
        r"id\s*=\s*(\w+)",
        r"user\s*id\s+(\w+)"
    ]

    for pattern in patterns:
        match = re.search(pattern, input_str, re.IGNORECASE)
        if match:
            user_id = match.group(1)
            return user_id

    return None


cache = Cache.instance("tfsa")


def run_tfsa_assistant_sync(user_input: str, thread_id: Optional[str] = None,
                            _: Optional[str] = DEFAULT_MODEL, *,
                            session_id: Optional[str] = None,
                            message_id: Optional[str] = None) -> tuple[str, AgentState]:
    """
    Run the TFSA LangGraph agent workflow synchronously.

    Args:
        user_input: The user's input query
        thread_id: Optional thread ID for conversation state management
        _: Optional model for conversation state management
        session_id: Conversation id (1 session -> many messages); stamped on every log event
        message_id: This turn's id; links invocation_start (input) to invocation_end (output)

    Returns:
        Latest assistant response text
        Final agent state after workflow execution
    """
    start_time = time.time()
    try:
        # Audit handler captures every LLM + tool call (full prompt/completion, args, tokens)
        # as pure-JSON CloudWatch lines. One instance per invocation -> per-run token totals.
        handler = AuditCallbackHandler(agent="tfsa", thread_id=thread_id,
                                       session_id=session_id, message_id=message_id)

        # Check cache first
        cached_response, state = _check_cache_initialize_state(user_input, thread_id)
        # Carry the conversation/turn ids into state so in-graph events (routing_decision,
        # node_error) can stamp them like the callback-emitted events do.
        state["session_id"] = session_id
        state["message_id"] = message_id
        handler.set_user(state.get("user_id"))
        if cached_response:
            cached_response, _ = _split_thinking(cached_response)  # in case an old entry has it
            # Still emit a start/end pair so every message has a mappable input+output.
            with audited_run(handler, user_input=user_input, message_id=message_id):
                handler.set_output(cached_response)
            state["trace"] = handler.get_trace()
            return cached_response, state

        # Execute workflow. The user query is captured structurally by the
        # invocation_start event emitted from audited_run() below.
        accumulated_state = state.copy()
        assistant_response_text = "No response generated"
        with audited_run(handler, user_input=user_input, message_id=message_id):
            try:
                for step in graph_app.stream(state, config={"callbacks": [handler]}):
                    for node, value in step.items():
                        # A node that returns no state update streams a None value; skip it so
                        # accumulated_state.update(None) can't raise.
                        if not value:
                            continue
                        # Update accumulated state with node value
                        accumulated_state.update(value)

                        # Emit node output as a structured event (queryable in
                        # CloudWatch Logs Insights by event_type / agent / node). Routed
                        # through the handler so it inherits session_id/message_id/user_id.
                        if 'messages' in value and value['messages']:
                            msg = value["messages"][-1]
                            handler._emit("agent_node_output", node=node,
                                          content=msg.get("content"))
            except Exception as e:
                logging.error(f"Error executing workflow: {str(e)}")
                handler._emit("node_error", node="workflow", error=str(e),
                              error_type=type(e).__name__, stage="workflow")
                # Return state with error message
                state["messages"].append({
                    "role": "system",
                    "content": f"Workflow execution failed: {str(e)}"
                })

                # Check for throttling or connection issues
                error_msg = f"Request could not be completed due to a system error: {str(e)}"
                if "ThrottlingException" in str(e):
                    error_msg = _build_rate_limit_fallback(user_input, datetime.datetime.now().year)
                elif "ValidationException" in str(e):
                    error_msg = f"Request validation failed: {str(e)}"

                # Ensure accumulated_state has the error message as an assistant response
                if "messages" not in accumulated_state or not isinstance(accumulated_state["messages"], list):
                    accumulated_state["messages"] = list(state.get("messages", []))

                # Try to extract any intermediate helper messages to give a partial response if possible
                fallback_parts = []
                for msg in accumulated_state.get("messages", []):
                    # If any agent had a partial result
                    role = msg.get("role")
                    if role in ["document_agent", "search_agent", "calculation_agent", "transaction_agent"] and msg.get(
                            "content"):
                        if msg["content"] not in fallback_parts:
                            fallback_parts.append(msg["content"])

                if fallback_parts:
                    partial_resp = "\n\n".join(fallback_parts)
                    error_msg = f"{partial_resp}\n\n---\n⚠️ Note: The request was interrupted due to a system issue: {str(e)}"
                    if "ThrottlingException" in str(e):
                        error_msg = f"{partial_resp}\n\n---\n⚠️ Note: The request was interrupted because the AI service is currently experiencing high demand. Please try again in a few moments."

                accumulated_state["messages"].append({
                    "role": "assistant",
                    "content": error_msg
                })

            # Save thread state
            if thread_id:
                thread_cache_key = f"thread_state_{thread_id}"
                cache.cache(thread_cache_key, accumulated_state)

            # Extract last assistant message
            assistant_msgs = [msg['content'] for msg in accumulated_state['messages']
                              if msg.get('role') == 'assistant']
            if assistant_msgs:
                assistant_response_text = f"{assistant_msgs[-1]}".strip()
                # Safety net: strip any <thinking> a node left in (keep it in the audit log).
                assistant_response_text, leaked_thinking = _split_thinking(assistant_response_text)
                if leaked_thinking:
                    handler._emit("agent_reasoning", node="output_filter",
                                  reasoning=leaked_thinking)
                if len(assistant_response_text) <= 0:
                    assistant_response_text = "No response generated"

            # Record final output + actual user_id (resolved during the run) for invocation_end.
            handler.set_user(accumulated_state.get("user_id"))
            handler.set_output(assistant_response_text)

        # Log token usage if MLflow tracing is enabled (best-effort; the audit handler above
        # is the authoritative token source in the AgentCore runtime).
        _log_token_usage()

        # Attach the structured trace AFTER the thread-state cache write above so it isn't
        # persisted back into thread state (and after audited_run closed, so latency_ms is set).
        accumulated_state["trace"] = handler.get_trace()
        return assistant_response_text, accumulated_state
    finally:
        logging.info("run_tfsa_assistant_sync finished in %.3f seconds", time.time() - start_time)


def _log_token_usage():
    """Log token usage from MLflow trace if available."""
    try:
        # Get the trace object just created
        last_trace_id = mlflow.get_last_active_trace_id()
        if last_trace_id:
            trace = mlflow.get_trace(trace_id=last_trace_id)

            # Print the token usage
            total_usage = trace.info.token_usage
            logging.info("== Total token usage: ==")
            logging.info(f"  Input tokens: {total_usage['input_tokens']}")
            logging.info(f"  Output tokens: {total_usage['output_tokens']}")
            logging.info(f"  Total tokens: {total_usage['total_tokens']}")

            # Print the token usage for each LLM call
            logging.info("\n== Token usage for each LLM call: ==")
            for span in trace.data.spans:
                if usage := span.get_attribute("mlflow.chat.tokenUsage"):
                    logging.info(f"{span.name}:")
                    logging.info(f"  Input tokens: {usage['input_tokens']}")
                    logging.info(f"  Output tokens: {usage['output_tokens']}")
                    logging.info(f"  Total tokens: {usage['total_tokens']}")
    except Exception as e:
        logging.warning(f"Could not log token usage: {str(e)}")


def _format_resp(struct: dict) -> str:
    """Formats a dictionary into a Server-Sent Event string."""
    return "data: " + json.dumps(struct) + "\n\n"


async def _stream_graph_events(graph: CompiledStateGraph, state: dict, queue: asyncio.Queue,
                               config: Optional[dict] = None):
    """Streams graph events into a queue and signals completion."""
    try:
        async for event in graph.astream_events(state, version="v2", config=config):
            await queue.put({"type": "graph_event", "event": event})
    except Exception as e:
        logging.error(f"Error during graph execution: {e}", exc_info=True)
        await queue.put({"type": "error", "error": e})
    finally:
        # Signal that the graph stream is finished
        await queue.put({"type": "done"})


async def run_tfsa_assistant_stream(user_input: str, thread_id: Optional[str] = None,
                                    model: Optional[str] = DEFAULT_MODEL, *,
                                    session_id: Optional[str] = None,
                                    message_id: Optional[str] = None) -> AsyncGenerator[str, None]:
    """
    Streaming wrapper that yields SSE text/event-stream fragments.
    Compatible with watsonx Orchestrate external-agent streaming spec.
    Includes a heartbeat to keep the connection alive.

    Args:
        user_input: The user's input query
        thread_id: Optional thread ID for conversation state management
        model: Optional model for conversation state management
        session_id: Conversation id (1 session -> many messages); stamped on every log event
        message_id: This turn's id; links invocation_start (input) to invocation_end (output)

    Yields:
        SSE formatted streaming responses
    """
    start_time = time.time()
    graph_task = None
    # Audit handler captures every LLM + tool call as pure-JSON CloudWatch lines.
    handler = AuditCallbackHandler(agent="tfsa", thread_id=thread_id,
                                   session_id=session_id, message_id=message_id)
    # invocation_id for this turn == message_id (fallback UUID) so start/end records join.
    audit_id = message_id or str(uuid.uuid4())
    audit_started = False
    audit_status = "success"

    try:
        # Check cache first, which also initializes the state dictionary
        cached_response, state = _check_cache_initialize_state(user_input, thread_id)
        # Carry the conversation/turn ids into state so in-graph events (routing_decision,
        # node_error) can stamp them like the callback-emitted events do.
        state["session_id"] = session_id
        state["message_id"] = message_id
        handler.set_user(state.get("user_id"))
        if cached_response:
            # Emit a start/end pair even on cache hits so every message has a mappable
            # input+output record (the live path emits these further below).
            handler._emit("invocation_start", invocation_id=audit_id, input=user_input)
            handler.set_output(cached_response)
            handler._emit("invocation_end", invocation_id=audit_id, status="success",
                          duration_ms=round((time.time() - start_time) * 1000, 1),
                          token_usage=handler.token_totals, output=cached_response)
            # To simulate a real stream, generate a single run ID to use for all chunks.
            run_id = f"run-{uuid.uuid4()}"

            # Split the cached response into chunks for streaming simulation
            cached_response, _ = _split_thinking(cached_response)  # in case an old entry has it
            chunk_size = 50  # Characters per chunk
            chunks = [cached_response[i:i + chunk_size] for i in range(0, len(cached_response), chunk_size)]

            # Yield each chunk with a small delay to simulate streaming
            is_first_chunk = True
            for chunk in chunks:
                if chunk:  # Only yield non-empty chunks
                    # The 'role' should only be sent in the first delta chunk of a message.
                    delta = {"content": chunk}
                    if is_first_chunk:
                        delta["role"] = "assistant"
                        is_first_chunk = False

                    struct = {
                        "id": run_id,
                        "object": "thread.message.delta",
                        "created": int(time.time()),
                        "thread_id": thread_id,
                        "model": model,
                        "choices": [{"delta": delta}],
                    }
                    yield _format_resp(struct)
                    # Add a small delay to simulate streaming
                    await asyncio.sleep(0.05)

            struct = {
                "id": str(uuid.uuid4()),
                "object": "thread.message.delta",
                "created": int(time.time()),
                "thread_id": thread_id,
                "model": model,
                "choices": [{"delta": {"content": "", "role": "assistant"}}],
            }
            yield _format_resp(struct)

            yield "data: [DONE]\n\n"
            return

        # This will hold the final state of the graph execution
        final_state = {}
        # Raw accumulated model output (may include <thinking>); used to detect tag boundaries.
        streamed_content = ""
        # Clean text actually emitted to the client (thinking stripped); avoids duplication.
        emitted_text = ""

        # --- Heartbeat and Streaming Logic ---
        event_queue = asyncio.Queue()
        graph_task = asyncio.create_task(
            _stream_graph_events(graph_app, state, event_queue,
                                 config={"callbacks": [handler]}))

        # User query is captured by the invocation_start event below.
        handler._emit("invocation_start", invocation_id=audit_id, input=user_input)
        audit_started = True

        while True:
            try:
                # Wait for an event from the graph, with a 5-second timeout.
                item = await asyncio.wait_for(event_queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                # If we time out, it means no graph event was received for 5 seconds.

                # Send a heartbeat to keep the connection alive and continue waiting:
                # The underlying protocol for these streams is Server-Sent Events (SSE). According to the SSE
                # specification, any line that begins with a colon (:) is treated as a comment and should be ignored
                # by the client. This is the standard and recommended way to implement a heartbeat or keep-alive
                # mechanism. It prevents client-side or proxy timeouts during long-running agent tasks without
                # interfering with the structured data events.
                yield ":heartbeat\n\n"
                continue

            item_type = item.get("type")

            if item_type == "error":
                # Propagate the error to the main exception handler
                raise item["error"]

            if item_type == "done":
                # The graph stream has finished, exit the loop
                break

            # Process the graph event
            event = item.get("event")
            if not event:
                continue

            kind = event.get("event")
            logging.debug(f"event = {event}")

            # --- Logic to stream events to the client ---
            if kind == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    if isinstance(content, list):
                        text_chunks = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                text_chunks.append(block.get("text", ""))
                            elif isinstance(block, str):
                                text_chunks.append(block)
                        content_str = "".join(text_chunks)
                    else:
                        content_str = str(content)

                    if content_str:
                        streamed_content += content_str
                        # Emit only text outside <thinking> blocks. _stream_safe_clean holds
                        # back a trailing partial tag so a tag split across chunks is never
                        # streamed; the withheld piece is released once the next chunk resolves it.
                        clean_so_far = _stream_safe_clean(streamed_content)
                        delta_str = clean_so_far[len(emitted_text):]
                        if delta_str:
                            emitted_text = clean_so_far
                            struct = {
                                "id": str(uuid.uuid4()),
                                "object": "thread.message.delta",
                                "created": int(time.time()),
                                "thread_id": thread_id,
                                "model": model,
                                "choices": [{"delta": {"content": delta_str, "role": "assistant"}}],
                            }
                            yield _format_resp(struct)

            elif kind == "on_tool_start":
                step_details = {
                    "type": "tool_calls",
                    "tool_calls": [{"id": event['run_id'], "name": event['name'], "args": event['data'].get('input')}]}
                struct = {
                    "id": str(uuid.uuid4()), "object": "thread.run.step.delta", "thread_id": thread_id,
                    "model": model, "created": int(time.time()),
                    "choices": [{"delta": {"role": "assistant", "step_details": step_details}}],
                }
                yield _format_resp(struct)

            elif kind == "on_tool_end":
                output = event.get('data', {}).get('output')
                content = json.dumps(output) if not isinstance(output, str) else output
                step_details = {
                    "type": "tool_response", "name": event['name'], "tool_call_id": event['run_id'], "content": content
                }
                struct = {
                    "id": str(uuid.uuid4()), "object": "thread.run.step.delta", "thread_id": thread_id,
                    "model": model, "created": int(time.time()),
                    "choices": [{"delta": {"role": "assistant", "step_details": step_details}}],
                }
                yield _format_resp(struct)

            # Capture the final state at the end of the graph run.
            # Robustly matches various top-level chain names (LangGraph, CompiledStateGraph, __root__)
            # and falls back to checking if the event has no parent_ids (which only the root chain does).
            if kind == "on_chain_end" and (
                    event.get("name") in ("LangGraph", "CompiledStateGraph", "__root__") or not event.get(
                "parent_ids")):
                if "output" in event["data"]:
                    final_state = event["data"]["output"]

        # After the stream is complete, extract the final response from the state.
        # This is necessary for agents that produce a final response without streaming it.
        if final_state:
            handler.set_user(final_state.get("user_id"))
            assistant_msgs = [msg['content'] for msg in final_state.get('messages', [])
                              if msg.get('role') == 'assistant']
            if assistant_msgs:
                final_response, final_thinking = _split_thinking(assistant_msgs[-1])
                if final_thinking:
                    handler._emit("agent_reasoning", node="output_filter", reasoning=final_thinking)
                handler.set_output(final_response)
                # Emit only the part not already streamed (nodes that returned a full answer
                # without token streaming, e.g. advisor_agent -> END).
                remainder = final_response[len(emitted_text):] if final_response.startswith(emitted_text) else final_response
                if remainder:
                    struct = {
                        "id": str(uuid.uuid4()),
                        "object": "thread.message.delta",
                        "created": int(time.time()), "thread_id": thread_id, "model": model,
                        "choices": [{"delta": {"content": remainder, "role": "assistant"}}],
                    }
                    yield _format_resp(struct)

        # Save the final state to the thread cache
        if thread_id and final_state:
            thread_cache_key = f"thread_state_{thread_id}"
            cache.cache(thread_cache_key, final_state)
            logging.info(f"Saved final state to cache for thread_id: {thread_id}")

        # Log token usage
        _log_token_usage()

        # Send the final [DONE] message correctly at the end of the stream
        yield "data: [DONE]\n\n"

    except Exception as e:
        audit_status = "error"
        if audit_started:
            handler._emit("invocation_error", invocation_id=audit_id,
                          error=str(e), error_type=type(e).__name__)
        logging.error(f"Error in run_tfsa_assistant_stream: {str(e)}", exc_info=True)
        error_message = f"An error occurred while processing your request: {str(e)}"
        yield f"data: {json.dumps({'choices': [{'delta': {'content': error_message}}]})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        # Emit the closing audit event with accumulated token totals (skips cached-only runs).
        if audit_started:
            handler._emit("invocation_end", invocation_id=audit_id, status=audit_status,
                          duration_ms=round((time.time() - start_time) * 1000, 1),
                          token_usage=handler.token_totals, output=handler._output)
        # Clean up background tasks to prevent them from running forever
        if graph_task:
            graph_task.cancel()
        logging.info("run_tfsa_assistant_stream finished in %.3f seconds", time.time() - start_time)


def _response_cache_key(user_input: str, user_id: Optional[str]) -> str:
    """Cache key for a final response, scoped by user_id so personalized answers (e.g. room
    calculations) are never served to a different user who asks the same question."""
    uid = user_id or "anon"
    return hashlib.sha256(f"{uid}::{user_input}".encode("UTF-8")).hexdigest()


def _check_cache_initialize_state(user_input: str, thread_id: Optional[str] = None) -> tuple[Optional[str], dict]:
    """
    Check if response is cached and return it if available.

    Args:
        user_input: The user's input query
        thread_id: Optional thread ID for conversation state management

    Returns:
        Cached response content or None if not cached
        Cached state or initialized state if not cached
    """
    # Retrieve thread state if exists
    # Create initial state
    state = {}

    # Initialize missing state fields
    state.setdefault("user_profile", None)
    state.setdefault("search_results", None)
    state.setdefault("contribution_room", None)
    state.setdefault("current_tfsa_limit", None)
    state.setdefault("contribution_amount", None)
    state.setdefault("intent", None)
    state.setdefault("messages", [])

    # Retrieve thread state if exists. Single load (no separate contains() check) that tolerates
    # a None return — the entry can expire/disappear between a contains() check and the load.
    if thread_id:
        thread_cache_key = f"thread_state_{thread_id}"
        cached = cache.load_from_cache(thread_cache_key)
        if cached and cached.get("value") is not None:
            state = cached["value"]
    state["user_input"] = user_input
    if "messages" not in state:
        state["messages"] = []

    # Add user message to history
    state["messages"].append({
        "role": "user",
        "content": state["user_input"]
    })

    # Extract user ID from input if not already set
    if not state.get("user_id"):
        user_id = extract_user_id(user_input)
        if user_id:
            state["user_id"] = user_id
        else:
            state["user_id"] = "unknown"

    # Look up a cached response, scoped by user so personalized answers aren't shared across
    # users. Single load that tolerates a None return (item can expire between check and load).
    cache_hash = _response_cache_key(user_input, state.get("user_id"))
    cache_item = cache.load_from_cache(cache_hash)
    if cache_item is not None:
        return cache_item.get("value", ""), state
    return None, state
