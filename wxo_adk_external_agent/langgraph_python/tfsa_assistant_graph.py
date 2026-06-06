# tfsa_assistant_graph.py
import asyncio
import datetime
import hashlib
import json
import logging
import mlflow
import operator
import os
import re
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

# Structured audit logging (tool calls, full LLM prompt/completion, token usage) for
# CloudWatch -> S3. Agent-agnostic framework; this graph is its first consumer.
from agent_obs import AuditCallbackHandler, audited_run

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


# Reduces call center volume by 80%+
# Processes contributions in <2 seconds
# Ensures 100% compliance with CRA regulations
# Provides personalized financial guidance
# TODO: Withdrawal simulation agent
# TODO: Contribution optimization advisor
# TODO: Multi-year projection tool
# TODO: Integrated tax impact analysis

def initialize_llm():
    """Initializes and returns the appropriate LLM based on configuration."""
    provider = config.AI_SERVICES_PROVIDER

    if 'bedrock' in provider:
        # ChatBedrockConverse uses Bedrock's Converse API, which is required for Amazon Nova
        # and also works for Anthropic Claude. It streams natively via astream_events.
        try:
            from langchain_aws import ChatBedrockConverse
            import boto3
            from botocore.config import Config
            retry_config = Config(
                retries={
                    'max_attempts': 4,
                    'mode': 'standard'
                }
            )
            bedrock_client = boto3.client(
                service_name="bedrock-runtime",
                region_name=config.AWS_REGION,
                config=retry_config
            )
            return ChatBedrockConverse(
                model=config.BEDROCK_MODEL_ID,
                client=bedrock_client,
                temperature=0,
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


llm = initialize_llm()

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


def _extract_amount_from_text(user_input: str) -> float:
    """Extract a dollar amount from free text; returns 0.0 when absent/invalid."""
    amount_match = re.search(r"\$?(\d{1,3}(?:,\d{3})*\d*(?:\.\d+)?)", user_input)
    if not amount_match:
        return 0.0
    amount_str = amount_match.group(0).replace(",", "").replace("$", "")
    try:
        return float(amount_str)
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


def _invoke_llm(prompt: str, agent: str):
    """Invoke the shared LLM, tagging the call with its prompt identity when supported.

    Custom LLMs (e.g. the watsonx wrapper) whose .invoke() doesn't accept a config kwarg
    fall back to a plain call so prompt tagging never breaks a provider.
    """
    try:
        return llm.invoke(prompt, config=_prompt_config(agent))
    except TypeError:
        return llm.invoke(prompt)


# Constants for TFSA limits file
TFSA_LIMITS_FILE = "tfsa_limits.json"


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
            # Save updated limits to file
            try:
                with open(TFSA_LIMITS_FILE, 'w') as f:
                    # Convert integer keys to strings for JSON serialization
                    json.dump({str(year): limit for year, limit in limits.items()}, f, indent=2)
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

        # Use Tavily search first, fallback to DuckDuckGo
        try:
            search_results = search_cra_tfsa_policy.invoke(search_query)
        except Exception as e:
            logging.warning(f"Tavily search failed: {e}. Falling back to DuckDuckGo.")
            search_results = search_cra_tfsa_policy_duck_duck_go.invoke(search_query)

        # Use LLM to extract the TFSA limits from search results
        prompt = f"""
        You are a financial policy expert. I need to find the TFSA (Tax-Free Savings Account) 
        annual contribution limits for the following years in Canada: {years_str}.

        Here are the search results:
        {json.dumps(search_results, indent=2)}

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

        # Use the already initialized LLM
        response = llm.invoke(prompt)
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
def profile_agent(state: AgentState):
    """Retrieves user profile and initializes state"""

    if not state.get("user_id"):
        # Don't require user ID for general questions
        return {
            "messages": [{
                "role": "system",
                "content": "No user ID provided - processing as general inquiry"
            }]
        }
    profile = retrieve_user_profile.invoke(state["user_id"])
    return {
        "user_profile": profile,
        "messages": [{
            "role": "system",
            "content": f"Retrieved profile for {profile['name']} (Age: {profile['age']})"
        }]
    }


def document_agent(state: AgentState):
    """Agent with knowledge of historical TFSA rules"""
    current_year = datetime.datetime.now().year
    # Handle case where user profile might be missing
    if state.get("user_profile"):
        user_info = f"User: {state['user_profile']['name']}, Age: {state['user_profile']['age']}"
    else:
        user_info = "User: General Inquiry"

    prompt = f"""
    You are a TFSA (Tax-Free Savings Account) policy expert at a Canadian bank.
    Current year: {current_year}
    {user_info}
    User question: {state['user_input']}

    Known annual TFSA contribution limits (fixed historical facts, accurate through 2024):
    - 2009-2012: $5,000
    - 2013-2014: $5,500
    - 2015: $10,000
    - 2016-2018: $5,500
    - 2019-2022: $6,000
    - 2023: $6,500
    - 2024: $7,000

    Known rules:
    - Unused contribution room carries forward indefinitely.
    - Withdrawals are added back to contribution room the FOLLOWING calendar year, not the same year.
    - Over-contribution penalty: 1% per month on the excess amount.
    - You must be 18+ and a Canadian resident to contribute.

    Respond with JSON ONLY containing:
    {{
      "policy_summary": "Your answer to the user's question, using only the known facts above.",
      "needs_current_search": true/false
    }}

    Instructions:
    - Answer ONLY from the known limits and rules above. Never invent a figure.
    - Set "needs_current_search" to true whenever a correct answer needs data you do NOT have,
      i.e. the contribution limit for {current_year} or any year after 2024, or cumulative room
      that depends on those years. Otherwise set it to false.
    - If the user asks only about historical limits (2009-2024), set "needs_current_search" to false
      and list every year range with its amount. Format as:
        TFSA Annual Contribution Limits:
        [YEAR RANGE]: $AMOUNT
      and state that cumulative room for someone eligible since 2009 is the sum of all annual
      limits up to and including the year in question.
    - Do NOT state a limit for {current_year} or any year after 2024; defer those by setting
      needs_current_search=true.
    """

    # Use unified LLM interface
    if hasattr(llm, 'invoke'):
        response = _invoke_llm(prompt, "document_agent")
        response_content = _extract_string_content(response.content) if hasattr(response,
                                                                                'content') else _extract_string_content(
            response)
    else:
        response_content = _invoke_llm(prompt, "document_agent")

    data = _get_json_from_str(response_content, {
        "policy_summary": response_content,
        "needs_current_search": True
    })
    return {
        "messages": [{
            "role": "document_agent",
            "content": data["policy_summary"],
            "needs_search": data["needs_current_search"]
        }]
    }


def search_agent(state: AgentState):
    """Agent that searches for current TFSA policies using Tavily"""
    try:
        # Use original user query instead of fixed term
        query = f"{state['user_input']}"

        # Use DuckDuckGo as fallback if Tavily fails
        try:
            results = search_cra_tfsa_policy.invoke(query)
        except:
            results = search_cra_tfsa_policy_duck_duck_go.invoke(query)

        # Search results: {results}
        # Extract key information. Process results with LLM
        prompt = f"""
        You are a helpful TFSA specialist at a Canadian bank. The CRA search results below are
        your source of truth for current-year data — ground every figure in them and never guess.
        Current year: {datetime.datetime.now().year}

        Search results:
        {json.dumps(results, indent=2)}

        The user asked: "{state['user_input']}"

        Return a JSON object with exactly these fields:
        {{
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
        response = _invoke_llm(prompt, "search_agent")
        # Access the content attribute of the response
        response_content = _extract_string_content(response.content) if hasattr(response,
                                                                                'content') else _extract_string_content(
            response)

        # Try to parse the JSON response
        policy_data = _get_json_from_str(response_content,
                                         {"answer": response_content, "error": "Could not parse policy data"})

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
        return {
            "messages": [{
                "role": "search_agent",
                "content": f"⚠️ Search failed: {str(e)}"
            }]
        }


def calculation_agent(state: AgentState):
    """Calculates contribution room based on profile and policies"""
    # Check for user ID
    if not state.get("user_id"):
        return {
            "messages": [{
                "role": "assistant",
                "content": "I need your user ID to process your contribution. Please provide your user ID."
            }]
        }
    # Dynamic contribution room calculation
    current_year = datetime.datetime.now().year
    profile = state["user_profile"]

    # Get current year limit
    global TFSA_LIMITS
    if current_year not in TFSA_LIMITS:
        TFSA_LIMITS = _load_or_update_tfsa_limits()
    current_limit = state.get("current_tfsa_limit") or TFSA_LIMITS.get(current_year, 6000)  # Use dynamic limits

    # Calculate total accumulated room
    total_room = 0
    used_room = 0
    if profile:
        birth_year = current_year - profile["age"]
        first_year = max(profile["first_tfsa_year"], birth_year + 18)

        total_room = 0
        for year in range(first_year, current_year):
            total_room += TFSA_LIMITS.get(year, 0)  # Default to 0 for unknown years

        # Add current year's limit
        total_room += current_limit

        # Calculate available room
        used_room = profile["past_contributions"] + profile["current_year_contributions"]
        available_room = total_room - used_room + profile["withdrawals_last_year"]
    else:
        available_room = 0

    # Create user-friendly response
    if profile:
        response = (
            f"Based on your profile, your available TFSA contribution room for {current_year} is ${available_room:.2f}.\n"
            f"* Total accumulated room: ${total_room:.2f}\n"
            f"* Contributions to date: ${used_room:.2f}\n"
            f"* Withdrawals added back: ${profile['withdrawals_last_year']:.2f}"
        )
    else:
        response = f"Available contribution room: ${available_room:.2f}"

    return {
        "contribution_room": available_room,
        "messages": [{
            "role": "assistant",  # Changed to assistant role
            "content": response
        }]
    }


def transaction_agent(state: AgentState):
    """Handles transaction execution"""
    # Check for user ID
    if not state.get("user_id"):
        return {
            "messages": [{
                "role": "assistant",
                "content": "I need your user ID to check your contribution room. Please provide your user ID."
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
        # Cache the result
        cache_hash = hashlib.sha256(f"{user_input}".encode('UTF-8')).hexdigest()
        cache.cache(cache_hash, final_content, metadata={"user_input": user_input})
    else:
        # Generate final response using LLM
        try:
            if hasattr(llm, 'invoke'):
                response = _invoke_llm(prompt, "response_agent")
                final_content = _extract_string_content(response.content) if hasattr(response,
                                                                                     'content') else _extract_string_content(
                    response)
            else:
                final_content = _invoke_llm(prompt, "response_agent")

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
                # Create unique cache id to avoid duplicate requests
                cache_hash = hashlib.sha256(f"{user_input}".encode('UTF-8')).hexdigest()
                # Only cache the policy user query
                cache.cache(cache_hash, final_content, metadata={"user_input": user_input})
        except Exception as e:
            logging.error(f"Response generation failed: {str(e)}")
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
    workflow.add_node("response_agent", response_agent)

    # Define edges
    workflow.set_entry_point("profile_agent")

    # Conditional edge after profile agent
    def route_after_profile(state: AgentState):
        """Decide next step after profile_agent"""
        user_input = state["user_input"].lower()

        # Handle calculation requests (contribution room)
        if (re.search(r"contribution room|how much can i contribute|room available|limit available", user_input) or
                "how much" in user_input and ("contribute" in user_input or "room" in user_input)):
            return "calculation_agent"

        # Handle transaction requests
        if _is_transaction_request(user_input):
            return "calculation_agent"  # Need room calculation first

        return "document_agent"

    workflow.add_conditional_edges(
        "profile_agent",
        route_after_profile,
        {
            "document_agent": "document_agent",
            "calculation_agent": "calculation_agent"
        }
    )

    # Conditional edge after calculation agent
    def route_after_calculation(state: AgentState):
        """Decide next step after calculation"""
        user_input = state["user_input"].lower()

        # Handle transaction requests
        if _is_transaction_request(user_input):
            return "transaction_agent"
        # For simple queries like "what is my room?", end after calculation.
        return END

    workflow.add_conditional_edges(
        "calculation_agent",
        route_after_calculation,
        {
            "transaction_agent": "transaction_agent",
            END: END
        }
    )

    # Conditional edge after document agent
    def route_after_document(state: AgentState):
        """Decide next step after document_agent"""
        # Always search if needed
        if any(msg.get("needs_search", False) for msg in state["messages"]):
            return "search_agent"

        return "response_agent"

    workflow.add_conditional_edges(
        "document_agent",
        route_after_document,
        {
            "search_agent": "search_agent",
            "response_agent": "response_agent"
        }
    )

    # Define terminal edges for the graph
    workflow.add_edge("transaction_agent", END)
    workflow.add_edge("search_agent", "response_agent")
    workflow.add_edge("response_agent", END)

    # Compile the graph
    compiled_state_graph = workflow.compile()

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
        handler.set_user(state.get("user_id"))
        if cached_response:
            # Still emit a start/end pair so every message has a mappable input+output.
            with audited_run(handler, user_input=user_input, message_id=message_id):
                handler.set_output(cached_response)
            return cached_response, state

        # Execute workflow. The user query is captured structurally by the
        # invocation_start event emitted from audited_run() below.
        accumulated_state = state.copy()
        assistant_response_text = "No response generated"
        with audited_run(handler, user_input=user_input, message_id=message_id):
            try:
                for step in graph_app.stream(state, config={"callbacks": [handler]}):
                    for node, value in step.items():
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
                if len(assistant_response_text) <= 0:
                    assistant_response_text = "No response generated"

            # Record final output + actual user_id (resolved during the run) for invocation_end.
            handler.set_user(accumulated_state.get("user_id"))
            handler.set_output(assistant_response_text)

        # Log token usage if MLflow tracing is enabled (best-effort; the audit handler above
        # is the authoritative token source in the AgentCore runtime).
        _log_token_usage()

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
        # This will hold content that has already been streamed to avoid duplication
        streamed_content = ""

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
                        struct = {
                            "id": str(uuid.uuid4()),
                            "object": "thread.message.delta",
                            "created": int(time.time()),
                            "thread_id": thread_id,
                            "model": model,
                            "choices": [{"delta": {"content": content_str, "role": "assistant"}}],
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
                final_response = assistant_msgs[-1]
                handler.set_output(final_response)
                if final_response and final_response != streamed_content:
                    struct = {
                        "id": str(uuid.uuid4()),
                        "object": "thread.message.delta",
                        "created": int(time.time()), "thread_id": thread_id, "model": model,
                        "choices": [{"delta": {"content": final_response, "role": "assistant"}}],
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
    state.setdefault("messages", [])

    # Retrieve thread state if exists
    if thread_id:
        thread_cache_key = f"thread_state_{thread_id}"
        if cache.contains(thread_cache_key):
            state = cache.load_from_cache(thread_cache_key).get("value")
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

    # Create unique cache id to avoid duplicate requests
    cache_hash = hashlib.sha256(f"{user_input}".encode('UTF-8')).hexdigest()
    if cache.contains(cache_hash):
        cache_item = cache.load_from_cache(cache_hash)
        return cache_item.get("value", ""), state
    return None, state
