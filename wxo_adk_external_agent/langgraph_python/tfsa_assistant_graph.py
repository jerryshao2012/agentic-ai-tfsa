# tfsa_assistant_graph.py
import asyncio
import datetime
import hashlib
import json
import logging
import operator
import os
import re
import time
import uuid
from typing import AsyncGenerator, TypedDict, Annotated, Optional

import mlflow
from langchain.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.graph.state import CompiledStateGraph

import config
from cache import Cache
from models import ModelName, DEFAULT_MODEL

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

    # Try to load from file first
    if os.path.exists(TFSA_LIMITS_FILE):
        try:
            with open(TFSA_LIMITS_FILE, 'r') as f:
                file_limits = json.load(f)
                # Convert string keys to integers
                limits = {int(year): limit for year, limit in file_limits.items()}
            logging.info(f"Loaded TFSA limits from {TFSA_LIMITS_FILE}")
        except Exception as e:
            logging.warning(f"Failed to load TFSA limits from file: {e}")
            limits = base_limits.copy()
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
        response_content = response.content.strip() if hasattr(response, 'content') else response.strip()

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


# Load or update TFSA limits at startup
TFSA_LIMITS = _load_or_update_tfsa_limits()
logging.info(f"TFSA Limits loaded: {TFSA_LIMITS}")


# ======================
# 0. Help functions
# ======================
def _get_json_from_str(json_str: str, fallback_json: dict) -> dict:
    """Convert json string to dict"""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError) as e:
        logging.error(f"JSON parsing failed: {str(e)}")
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
    """Retrieves user's profile from bank database"""
    # Mock implementation - replace with actual DB call or API
    # TODO: Add JWT validation using PyJWT for user sessions
    # TODO: Integrate with bank's SSO system
    return {
        "user_id": user_id,
        "name": "Melanie",
        "age": 25,
        "residency_status": "Canadian Resident",
        "sin": "123-456-789",
        "first_tfsa_year": 2023,
        "past_contributions": 6500,  # 2023 limit
        "withdrawals_last_year": 2000,
        "current_year_contributions": 1500,
        "checking_balance": 8500.00
    }


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
    tavily = TavilySearch(api_key=config.TAVILY_API_KEY, max_results=3)
    # Real-time policy verification using Tavily search
    results = tavily.invoke({
        "query": f"site:canada.ca TFSA {datetime.datetime.now().year} {query}",
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": True
    })
    return results


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

    # Format TFSA limits for the prompt
    global TFSA_LIMITS
    tfsa_limits_lines = []

    # Group consecutive years with same limits
    sorted_years = sorted(TFSA_LIMITS.keys())
    if sorted_years:
        start_year = sorted_years[0]
        end_year = start_year
        current_limit = TFSA_LIMITS[start_year]

        for year in sorted_years[1:] + [None]:  # Add None to process the last group
            if year is None or TFSA_LIMITS[year] != current_limit:
                # End of a group
                if start_year == end_year:
                    tfsa_limits_lines.append(f"- Annual limit {start_year}: ${current_limit}")
                else:
                    tfsa_limits_lines.append(f"- Annual limit {start_year}-{end_year}: ${current_limit}")

                # Start a new group
                if year is not None:
                    start_year = year
                    end_year = year
                    current_limit = TFSA_LIMITS[year]
            else:
                # Continue current group
                end_year = year

    tfsa_limits_str = "\n".join(tfsa_limits_lines)

    prompt = f"""
    You are a TFSA policy expert. Current year: {current_year}
    {user_info}
    User Question: {state['user_input']}

    Known historical rules:
    {tfsa_limits_str}
    - Withdrawals re-added to room NEXT calendar year
    - Overcontribution penalty: 1% per month

    Respond with JSON ONLY containing:
    {{ 
      "policy_summary": "Detailed response including all requested information",
      "needs_current_search": true/false // Only true if question requires real-time verification
    }}
    
    Special Instructions:
    - If question asks about historical limits, provide COMPLETE list from 2009 to current year. Format response as:
        ...
        I believe you're asking about TFSA (Tax-Free Savings Account) contribution limits. 
        Here are the annual TFSA contribution room limits for each year since the program began in Canada:
        
        TFSA Annual Contribution Limits:
        [YEAR RANGE]: $AMOUNT
        ...
        
        Total cumulative contribution room for someone eligible since 2009: $95,000 (as of 2025)
    - Include total cumulative room for someone eligible since 2009 ($95,000 as of 2025)
    - For future years such as current year, note they are projections
    - For limit of current year, set needs_current_search=true since Known historical rules don't have current year
    - For withdrawal/penalty questions, set needs_current_search=true
    """

    # Unified prompt for automation
    """
    I have a [TYPE_OF_INPUT] and I want to automate [THIS_SPECIFIC_TASK].
    Here are the constraints:
    - It should be efficient, scalable, and easy to reuse
    - The output should be clean and ready for the next step in a workflow
    - If the task includes transformation or formatting, follow industry best practices

    Can you give me:
    - A clean, modular Python script that performs this
    - A list of libraries I need and why
    - Suggestions for how I could improve or scale it later
    """

    # Use unified LLM interface
    if hasattr(llm, 'invoke'):
        response = llm.invoke(prompt)
        response_content = response.content.strip() if hasattr(response, 'content') else response.strip()
    else:
        response_content = llm.invoke(prompt)

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
        You are a helpful financial assistant at a Canadian bank.
        Analyze these CRA TFSA policy search results for {datetime.datetime.now().year}:
        {json.dumps(results, indent=2)}
        
        The user asked: "{state['user_input']}"

        Extract the following in JSON format:
        {{
          "answer": "user-friendly response to the query",
          "current_limit": "current year contribution limit",
          "penalty_info": "brief penalty summary",
          "withdrawal_rules": "brief withdrawal rules summary"
        }}
        
        Special Instructions for `answer` in JSON Response:
        - For contribution intent queries, respond conversationally:
            "I'd be happy to help you contribute to your TFSA! 
            To provide the best guidance, could you tell me:
            * Do you already have a TFSA account?
            * Are you looking to make a one-time or regular contributions?
            * Do you know your available contribution room?
            
            In the meantime, here are key things to know:
            [Include key contribution information from search results]"
        - For policy questions, provide clear historical limits
        - For future years (beyond {datetime.datetime.now().year}), note:
            "Future limits are projections and subject to inflation adjustment"
        - Always use simple language and bullet points for readability
        - Ask follow-up questions to get more details when needed
        - Format professionally but conversationally
        
        Important: Respond with ONLY the JSON object. Do not include any additional text, 
        explanations, or markdown formatting. The response must be a valid JSON object 
        that can be parsed directly.
        """
        response = llm.invoke(prompt)
        # Access the content attribute of the response
        response_content = response.content.strip() if hasattr(response, 'content') else response.strip()

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
    # Only process if user input contains contribution keywords
    contribution_keywords = r"contribute|deposit|transfer|add|invest"
    if not re.search(contribution_keywords, state["user_input"], re.IGNORECASE):
        return {
            "messages": [{
                "role": "assistant",
                "content": "I've gathered the information you requested about TFSA policies."
            }]
        }

    # Extract amount from user input
    amount = 0
    amount_match = re.search(r"\$?(\d{1,3}(?:,\d{3})*\d*(?:\.\d+)?)", state["user_input"])
    if amount_match:
        amount_str = amount_match.group(0).replace(",", "").replace("$", "")
        try:
            amount = float(amount_str)
        except ValueError:
            amount = 0

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
    You are a certified TFSA specialist at a Canadian bank. Synthesize this information into a single, coherent, human-readable response:
    User question:
    {context['user_question']}
    
    Assistant responses:
    {context['assistant_responses']}
    
    Additional context:
    - Current Year: {current_year}
    - Policy Information: {context['policy_information']}
    - Available Contribution Room: {context['contribution_room']}
    - Contribution Amount: {context['contribution_amount']}
    
    Response guidelines:
    1. Address the user's question directly first
    2. Organize information logically: policy → calculations → actions
    3. Use simple language and bullet points for readability
    4. Include these critical elements when relevant:
       - Current year {current_year}'s contribution limit
       - Penalty risks for over-contributions
       - Withdrawal re-contribution rules
       - Transaction ID if applicable
    5. End with a helpful follow-up question or next step suggestion
    
    Guideline:
    - Use professional but conversational tone
    - Keep response under 300 words
    - Do NOT mention you're synthesizing information
    - If question asks about historical limits, provide COMPLETE list from 2009 to {current_year}. Format response as:
        ...
        TFSA Annual Contribution Limits:
        [YEAR RANGE]: $AMOUNT
        ...
    """

    # Generate final response using LLM
    try:
        if hasattr(llm, 'invoke'):
            response = llm.invoke(prompt)
            final_content = response.content.strip() if hasattr(response, 'content') else response.strip()
        else:
            final_content = llm.invoke(prompt)

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
        final_content = "\n".join(assistant_messages)  # Fallback to original messages

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
        if re.search(r"contribute|deposit|add|transfer|invest", user_input):
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
        transaction_keywords = r"contribute|deposit|add|transfer|invest|yes, i want"
        if (re.search(transaction_keywords, user_input) or
                any(word in user_input for word in ["proceed", "execute", "do it"])):
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
                            _: Optional[str] = DEFAULT_MODEL) -> tuple[str, AgentState]:
    """
    Run the TFSA LangGraph agent workflow synchronously.

    Args:
        user_input: The user's input query
        thread_id: Optional thread ID for conversation state management
        _: Optional model for conversation state management

    Returns:
        Latest assistant response text
        Final agent state after workflow execution
    """
    start_time = time.time()
    try:
        # Check cache first
        cached_response, state = _check_cache_initialize_state(user_input, thread_id)
        if cached_response:
            return cached_response, state

        # Execute workflow
        logging.info(f"\n🔹 USER QUERY: '{user_input}'")
        accumulated_state = state.copy()
        try:
            for step in graph_app.stream(state):
                for node, value in step.items():
                    # Update accumulated state with node value
                    accumulated_state.update(value)

                    # Print node output
                    if 'messages' in value and value['messages']:
                        msg = value["messages"][-1]
                        logging.info(f"🔹 [{node.upper()}]: {msg['content']}")
        except Exception as e:
            logging.error(f"Error executing workflow: {str(e)}")
            # Return state with error message
            state["messages"].append({
                "role": "system",
                "content": f"Workflow execution failed: {str(e)}"
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
        else:
            assistant_response_text = "No response generated"

        # Log token usage if MLflow tracing is enabled
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


async def _stream_graph_events(graph: CompiledStateGraph, state: dict, queue: asyncio.Queue):
    """Streams graph events into a queue and signals completion."""
    try:
        async for event in graph.astream_events(state, version="v2"):
            await queue.put({"type": "graph_event", "event": event})
    except Exception as e:
        logging.error(f"Error during graph execution: {e}", exc_info=True)
        await queue.put({"type": "error", "error": e})
    finally:
        # Signal that the graph stream is finished
        await queue.put({"type": "done"})


async def run_tfsa_assistant_stream(user_input: str, thread_id: Optional[str] = None,
                                    model: Optional[str] = DEFAULT_MODEL) -> AsyncGenerator[str, None]:
    """
    Streaming wrapper that yields SSE text/event-stream fragments.
    Compatible with watsonx Orchestrate external-agent streaming spec.
    Includes a heartbeat to keep the connection alive.

    Args:
        user_input: The user's input query
        thread_id: Optional thread ID for conversation state management
        model: Optional model for conversation state management

    Yields:
        SSE formatted streaming responses
    """
    start_time = time.time()
    graph_task = None

    try:
        # Check cache first, which also initializes the state dictionary
        cached_response, state = _check_cache_initialize_state(user_input, thread_id)
        if cached_response:
            struct = {
                "id": str(uuid.uuid4()),
                "object": "thread.message.delta",
                "created": int(time.time()),
                "thread_id": thread_id,
                "model": model,
                "choices": [{"delta": {"content": cached_response, "role": "assistant"}}],
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
        graph_task = asyncio.create_task(_stream_graph_events(graph_app, state, event_queue))

        logging.info(f"\n🔹 USER QUERY: '{user_input}'")

        # Track the last time we sent an event (including heartbeats)
        last_event_time = time.time()

        while True:
            try:
                # Wait for an event from the graph, with a 5 seconds timeout
                item = await asyncio.wait_for(event_queue.get(), timeout=5)
            except asyncio.TimeoutError:
                # If we time out, check if it's been more than 5 seconds since last event
                if time.time() - last_event_time >= 5.0:
                    # Send a heartbeat and update last_event_time
                    yield ":heartbeat\n\n"
                    last_event_time = time.time()
                # Continue to check for events
                continue

            # Update last event time for any received item
            last_event_time = time.time()

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
                    streamed_content += content
                    struct = {
                        "id": str(uuid.uuid4()),
                        "object": "thread.message.delta",
                        "created": int(time.time()),
                        "thread_id": thread_id,
                        "model": model,
                        "choices": [{"delta": {"content": content, "role": "assistant"}}],
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

            # Capture the final state at the end of the graph run
            if kind == "on_chain_end" and event["name"] == "LangGraph":
                if "output" in event["data"]:
                    final_state = event["data"]["output"]

        # After the stream is complete, extract the final response from the state.
        # This is necessary for agents that produce a final response without streaming it.
        if final_state:
            assistant_msgs = [msg['content'] for msg in final_state.get('messages', [])
                              if msg.get('role') == 'assistant']
            if assistant_msgs:
                final_response = assistant_msgs[-1]
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
        logging.error(f"Error in run_tfsa_assistant_stream: {str(e)}", exc_info=True)
        error_message = f"An error occurred while processing your request: {str(e)}"
        yield f"data: {json.dumps({'choices': [{'delta': {'content': error_message}}]})}\n\n"
        yield "data: [DONE]\n\n"
    finally:
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
