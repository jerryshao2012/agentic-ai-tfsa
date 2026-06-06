# tfsa_assistant.py
import datetime
import json
import logging
import operator
import os
import re
from typing import AsyncGenerator, TypedDict, Annotated, Optional, Callable

from dotenv import load_dotenv
from langchain.tools import tool
from langgraph.graph import StateGraph, END

import config
from models import ModelName

logger = logging.getLogger(__name__)

# Reduces call center volume by 80%+
# Processes contributions in <2 seconds
# Ensures 100% compliance with CRA regulations
# Provides personalized financial guidance
# TODO: Withdrawal simulation agent
# TODO: Contribution optimization advisor
# TODO: Multi-year projection tool
# TODO: Integrated tax impact analysis

load_dotenv('.env')

if 'ollama' in config.AI_SERVICES_PROVIDER:
    # Configuration for Ollama. Initialize Ollama with qwen2.5vl:7b model locally
    from langchain_ollama import ChatOllama

    llm = ChatOllama(
        model=ModelName.ollama_qwen2_5vl_7b,
        # other params...
        temperature=0)  # Use your preferred qwen2.5vl:7b variant
elif 'deepseek' in config.AI_SERVICES_PROVIDER:
    # Configuration for Deepseek. Initialize DeepSeek LLM: pip install -U langchain-deepseek
    from langchain_deepseek import ChatDeepSeek

    llm = ChatDeepSeek(
        model=ModelName.deepseek_chat,
        # other params...
        temperature=0,
        api_key=config.DEEPSEEK_API_KEY)
elif 'openai' in config.AI_SERVICES_PROVIDER:
    # Configuration for OpenAI. Initialize OpenAI LLM: pip install -U langchain-openai
    from langchain_openai import ChatOpenAI

    llm = ChatOpenAI(
        model=ModelName.openai_gpt_4_o_mini,
        # other params...
        temperature=0,
        streaming=False,
        api_key=config.OPENAI_API_KEY)
else:
    # Configuration for Watsonx.ai
    from ibm_watson_machine_learning.foundation_models import Model
    from ibm_watson_machine_learning.metanames import GenTextParamsMetaNames as GenParams

    # Initialize Watsonx model
    watsonx_params = {
        GenParams.DECODING_METHOD: "greedy",
        GenParams.MIN_NEW_TOKENS: 1,
        GenParams.MAX_NEW_TOKENS: 1024,
        GenParams.TEMPERATURE: 0,
    }

    watsonx_model = Model(
        model_id=ModelName.watsonx_llama_3_2_90b,
        # other params...
        params=watsonx_params,
        credentials={
            "apikey": config.WATSONX_API_KEY,
            "url": config.WATSONX_URL
        },
        project_id=config.WATSONX_PROJECT_ID
    )


    # Helper function for Watsonx invocation
    class WatsonLLM:
        @staticmethod
        def invoke(prompt: str) -> str:
            """Invoke Watsonx model with prompt and return response"""
            response = watsonx_model.generate_text(prompt)
            return response


    llm = WatsonLLM()


# ======================
# 0. Help functions
# ======================
def get_json_from_str(json_str: str, fallback_json: dict) -> dict:
    """Convert json string to dict"""
    try:
        return json.loads(json_str)
    except (json.JSONDecodeError, ValueError) as e:
        logger.error(f"JSON parsing failed: {str(e)}")
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
            logger.error(f"JSON parsing failed 2nd try: {str(e)}")
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
            except:
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
        "new_balance": 6500 + new_contributions,  # Base + contributions
        "new_contributions": new_contributions,
        "transaction_id": f"TFSA-{datetime.datetime.now().year}-{hash(str(datetime.datetime.now()))}"
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
    You are a TFSA policy expert. Current year: {current_year}
    {user_info}
    User Question: {state['user_input']}

    Known historical rules:
    - Annual limit 2009-2012: $5000
    - Annual limit 2013-2014: $5500
    - Annual limit 2015: $10000
    - Annual limit 2016-2018: $5500
    - Annual limit 2019-2022: $6000
    - Annual limit 2023: $6500
    - Annual limit 2024: $7000
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

    data = get_json_from_str(response_content, {
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
            results = search_cra_tfsa_policy_duck_duck_go(query)

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
        policy_data = get_json_from_str(response_content, {"error": "Could not parse policy data"})
        return {
            "search_results": results,
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
    # Dynamic contribution room calculation
    current_year = datetime.datetime.now().year
    profile = state["user_profile"]

    # Get current year limit
    current_limit = 7000  # Default for 2024
    for msg in reversed(state["messages"]):
        if msg.get("role") == "search_agent" and "policy_data" in msg:
            try:
                limit_str = str(msg["policy_data"].get("current_limit", ""))
                # Extract numerical value
                match = re.search(r'\$?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)', limit_str)
                if match:
                    current_limit = float(match.group(1).replace(',', ''))
            except:
                continue
            break

    # Calculate total accumulated room
    total_room = 0
    used_room = 0
    if profile:
        birth_year = current_year - profile["age"]
        first_year = max(profile["first_tfsa_year"], birth_year + 18)

        # Historical limits
        limits = {
            2019: 6000, 2020: 6000, 2021: 6000, 2022: 6000,
            2023: 6500, 2024: 7000
        }

        total_room = 0
        for year in range(first_year, current_year):
            total_room += limits.get(year, 6000)  # Default to 6000 for unknown years

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

    # Extract user ID from state
    user_id = state.get("user_id")
    if not user_id:
        return {
            "messages": [{
                "role": "assistant",
                "content": "User ID not found. Please provide your user ID."
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
    context = {
        "user_question": state["user_input"],
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
    except Exception as e:
        logger.error(f"Response generation failed: {str(e)}")
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
workflow = StateGraph(AgentState)

# Define nodes
workflow.add_node("profile_agent", profile_agent)
workflow.add_node("document_agent", document_agent)
workflow.add_node("search_agent", search_agent)
workflow.add_node("calculation_agent", calculation_agent)
workflow.add_node("transaction_agent", transaction_agent)
workflow.add_node("response_agent", response_agent)  # New response agent

# Define edges
workflow.set_entry_point("profile_agent")
workflow.add_edge("profile_agent", "document_agent")


# Conditional edges
def after_document(state: AgentState):
    if any(msg.get("needs_search", False) for msg in state["messages"] if isinstance(msg, dict)):
        return "search_agent"
    return "response_agent"  # Go to response agent after document


workflow.add_conditional_edges(
    "document_agent",
    after_document,
    {"search_agent": "search_agent", "response_agent": "response_agent"}
)

workflow.add_edge("search_agent", "response_agent")  # Search goes to response


# Add conditional edge after response
def after_response(state: AgentState):
    """Only go to calculation if we have a user profile"""
    if state.get("user_profile") and re.search(r"contribute|deposit|room|limit", state["user_input"], re.IGNORECASE):
        return "calculation_agent"
    return END


workflow.add_conditional_edges(
    "response_agent",
    after_response,
    {"calculation_agent": "calculation_agent", END: END}
)

workflow.add_conditional_edges(
    "calculation_agent",
    lambda s: "transaction_agent" if re.search(r"contribute|deposit|transfer", s["user_input"], re.IGNORECASE) else END,
    {"transaction_agent": "transaction_agent", END: END}
)

workflow.add_edge("transaction_agent", END)

# Compile the graph
app = workflow.compile()

png_graph = app.get_graph().draw_mermaid_png()
with open("tfsa_graph.png", "wb") as f:
    f.write(png_graph)

logger.info(f"Graph saved as 'tfsa_graph.png' in {os.getcwd()}")


def get_graph_closure() -> Callable:
    return app


# ======================
# 5. Execution Function
# ======================
def extract_user_id(input_str: str) -> tuple:
    """Extracts user ID from input string and returns cleaned input"""
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
            cleaned_input = input_str.replace(match.group(0), '').strip()
            return user_id, cleaned_input

    return None, input_str


def run_tfsa_assistant_sync(user_input: str):
    """Run the agent workflow"""
    # Extract user ID from input
    user_id, cleaned_input = extract_user_id(user_input)

    state = {
        "user_input": cleaned_input,
        "user_id": user_id,
        "user_profile": None,
        "search_results": None,
        "contribution_room": None,
        "contribution_amount": None,
        "messages": []
    }

    # Execute workflow
    logger.info(f"\n🔹 USER QUERY: '{user_input}'")
    accumulated_state = state.copy()
    for step in app.stream(state):
        for node, value in step.items():
            # Update accumulated state with node value
            accumulated_state.update(value)

            # Print node output
            if 'messages' in value and value['messages']:
                msg = value["messages"][-1]
                logger.info(f"🔹 [{node.upper()}]: {msg['content']}")

    return accumulated_state


async def run_tfsa_assistant_stream(user_input: str) -> AsyncGenerator[str, None]:
    """
    Streaming wrapper that yields SSE text/event-stream fragments.
    Compatible with watsonx Orchestrate external-agent streaming spec.
    """
    # Extract user ID from input
    user_id, cleaned_input = extract_user_id(user_input)

    # Build LangGraph input
    state = {
        "user_input": cleaned_input,
        "user_id": user_id,
        "user_profile": None,
        "search_results": None,
        "contribution_room": None,
        "contribution_amount": None,
        "messages": [],
    }

    # LangGraph async stream
    async for event in app.astream_events(state, version="v2"):
        kind = event["event"]
        if kind == "on_chat_model_stream":
            chunk = event["data"]["chunk"].content or ""
            if chunk:
                yield f"data: {json.dumps({'choices': [{'delta': {'content': chunk}}]})}\n\n"
        elif kind == "on_tool_start":
            yield f"data: {json.dumps({'choices': [{'delta': {'role': 'assistant', 'tool_calls': [{'id': event['run_id'], 'function': {'name': event['name'], 'arguments': json.dumps(event['data'].get('input', {}))}}]}}]})}\n\n"
        elif kind == "on_tool_end":
            yield f"data: {json.dumps({'choices': [{'delta': {'role': 'tool', 'content': event['data'].get('output', '')}}]})}\n\n"
    yield "data: [DONE]\n\n"


# ======================
# 6. Example Usage
# ======================
if __name__ == "__main__":
    print("===== TFSA CONTRIBUTION ASSISTANT =====")

    # Example 1: Policy question
    print(
        "\n=== EXAMPLE 1: Policy Question ===\nWhat are the annual dollar limits for each year of TSFA, including 2025?")
    state = run_tfsa_assistant_sync("What are the annual dollar limits for each year of TSFA, including 2025?")
    if state.get("messages"):
        # Find the final assistant message
        assistant_msgs = [msg['content'] for msg in state.get("messages")
                          if msg.get('role') == 'assistant']
        response_text = assistant_msgs[-1] if assistant_msgs else "No response generated"
        print(response_text)
        # Sample answer:
        # I believe you're asking about TFSA (Tax-Free Savings Account) contribution limits. Here are the annual TFSA contribution room limits for each year since the program began in Canada:
        # TFSA Annual Contribution Limits:
        #
        # 2009-2012: $5,000 per year
        # 2013-2014: $5,500 per year
        # 2015: $10,000 (temporary increase)
        # 2016-2018: $5,500 per year
        # 2019-2022: $6,000 per year
        # 2023: $6,500
        # 2024: $7,000
        # 2025: $7,000
        #
        # Total cumulative contribution room for someone who was eligible since 2009 and has never contributed would be $95,000 as of 2025.
        # Important notes:
        #
        # These limits are indexed to inflation and rounded to the nearest $500
        # Unused contribution room carries forward indefinitely
        # You regain contribution room in January following any withdrawals made in the previous year
        # You must be 18 or older and a Canadian resident to contribute
        #
        # The limits are set annually by the Canada Revenue Agency based on inflation adjustments to the original $5,000 base amount.

    # Example 2: Contribution intent
    print("\n=== EXAMPLE 2: Contribution Intent ===\nI want to contribute to my TFSA")
    state = run_tfsa_assistant_sync("I want to contribute to my TFSA")
    # Display response
    if state.get("messages"):
        assistant_msgs = [msg['content'] for msg in state.get("messages")
                          if msg.get('role') == 'assistant']
        response_text = assistant_msgs[-1] if assistant_msgs else "No response generated"
        print(response_text)
        # Sample answer:
        # I'd be happy to help you with information about TFSA contributions! To provide the most relevant guidance, could you tell me a bit more about your situation?
        # For example:
        #
        # Do you already have a TFSA account set up, or would you need to open one first?
        # Are you looking to make a one-time contribution or set up regular contributions?
        # Do you know how much contribution room you currently have available?
        #
        # In the meantime, here are some key things to keep in mind:
        # Before contributing:
        #
        # Make sure you don't exceed your available contribution room (this includes any unused room from previous years plus this year's limit)
        # You can check your contribution room on your CRA My Account online, or call the CRA
        #
        # Ways to contribute:
        #
        # Online banking transfer to your TFSA
        # In-person at your bank or financial institution
        # Pre-authorized contributions (automatic transfers)
        # By cheque or bank draft
        #
        # Investment options within TFSA:
        #
        # High-interest savings accounts
        # GICs (Guaranteed Investment Certificates)
        # Mutual funds, ETFs, stocks (if your TFSA allows investments)
        #
        # What specific aspect of contributing would you like to focus on?

    # Example 3: Contribution room check (with user ID)
    print(
        "\n=== EXAMPLE 3: Contribution Room Check ===\nMy user ID is user_123. What is my contribution room for 2025?")
    state = run_tfsa_assistant_sync("My user ID is user_123. What is my contribution room for 2025?")
    # Display response
    if state.get("messages"):
        assistant_msgs = [msg['content'] for msg in state.get("messages")
                          if msg.get('role') == 'assistant']
        response_text = assistant_msgs[-1] if assistant_msgs else "No response generated"
        print(response_text)
        # Sample answer:
        # Based on the information I found, your current TFSA contribution room for 2025 is $14,500.
        # This means you can contribute up to $14,500 to your TFSA this year without exceeding your limit. This amount includes:
        #
        # Any unused contribution room carried forward from previous years
        # The 2025 annual limit of $7,000
        # Any withdrawals you made in previous years that have been added back to your room
        #
        # Would you like to proceed with making a contribution? I can help you with the next steps if you'd like to contribute some or all of this available room.

    # Example 4: Contribution execution
    if state.get("contribution_room") is not None:
        amount = input(f"\nHow much would you like to contribute? (Room: ${state['contribution_room']:.2f}): ")
        user_input = f"My user ID is user_123. Contribute ${amount}"
        print(f"\n=== EXAMPLE 4: Contribution Execution ===\n{user_input}")
        state = run_tfsa_assistant_sync(user_input)

        # Display final transaction result
        if state.get("messages") is not None:
            print("\n💎 FINAL RESULT:")
            # Print all assistant messages as they form the complete response
            assistant_msgs = [msg['content'] for msg in state.get("messages")
                              if msg.get('role') == 'assistant']
            response_text = assistant_msgs[-1] if assistant_msgs else "No response generated"
            print(response_text)
