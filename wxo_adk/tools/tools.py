"""
tools.py – TFSA Assistant tools for watsonx Orchestrate
"""
from __future__ import annotations

import datetime
import os
from typing import Any, Dict

from ibm_watsonx_orchestrate.agent_builder.connections import ConnectionType, ExpectedCredentials
from ibm_watsonx_orchestrate.agent_builder.tools import tool, ToolPermission
from ibm_watsonx_orchestrate.run import connections
# TODO: Withdrawal simulation agent
# TODO: Contribution optimization advisor
# TODO: Multi-year projection tool
# TODO: Integrated tax impact analysis
###############################################################################
# Helper LLM for policy extraction (watsonx.ai)
###############################################################################
from langchain_ibm import WatsonxLLM


def _get_llm() -> WatsonxLLM:
    """Return a watsonx LLM instance (granite-13b as default)."""
    return WatsonxLLM(
        model_id="ibm/granite-13b-instruct-v2",
        url=os.getenv("WATSONX_URL", "https://us-south.ml.cloud.ibm.com"),
        apikey=os.getenv("WATSONX_API_KEY"),
        project_id=os.getenv("WATSONX_PROJECT_ID"),
        params={"decoding_method": "greedy",
                "max_new_tokens": 512,
                "temperature": 0}
    )


###############################################################################
# 1. Profile lookup
###############################################################################
@tool(name="retrieve_user_profile", permission=ToolPermission.READ_ONLY)
def retrieve_user_profile(user_id: str) -> Dict[str, Any]:
    """Retrieve the user’s profile from the bank database."""
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
        "past_contributions": 6500,
        "withdrawals_last_year": 2000,
        "current_year_contributions": 1500,
        "checking_balance": 8500.00
    }


###############################################################################
# 2. CRA real-time policy search
###############################################################################
@tool(name="search_cra_tfsa_policy",
      permission=ToolPermission.READ_ONLY,
      expected_credentials=[ExpectedCredentials(
          app_id="tavily_search",
          type=ConnectionType.API_KEY_AUTH
      )])
def search_cra_tfsa_policy(query: str) -> Dict[str, Any]:
    """
    Search the Canada Revenue Agency (CRA) website for the most recent TFSA
    policy information and return structured JSON.
    """
    # pip install -U langchain-tavily
    from langchain_tavily import TavilySearch
    import json

    tavily_search_connection = connections.api_key_auth("tavily_search")
    tavily = TavilySearch(tavily_api_key=tavily_search_connection.api_key, max_results=3)
    current_year = datetime.datetime.now().year
    # Real-time policy verification using Tavily search
    results = tavily.invoke({
        "query": f"""site:canada.ca * TFSA {current_year} contribution limit;\n* {query}""",
        "search_depth": "advanced",
        "include_answer": True,
        "include_raw_content": True
    })

    return {
        "current_year": current_year,
        "search_results": json.dumps(results, indent=2)
    }


###############################################################################
# 3. Contribution transaction
###############################################################################
@tool(name="execute_tfsa_contribution", permission=ToolPermission.WRITE_ONLY)
def execute_tfsa_contribution(user_id: str, amount: float) -> Dict[str, Any]:
    """Execute a TFSA contribution transaction from the user’s chequing account."""
    # Mock implementation - replace with banking API
    # TODO: Encrypt PII data using AES-256
    # TODO: Add transaction confirmation step
    # TODO: Implement fraud detection hooks
    profile = retrieve_user_profile(user_id)
    if amount > profile["checking_balance"]:
        return {"status": "failed", "reason": "Insufficient funds"}

    new_contributions = profile["current_year_contributions"] + amount
    return {
        "status": "success",
        "new_balance": 6500 + new_contributions,
        "new_contributions": new_contributions,
        "transaction_id": f"TFSA-{datetime.datetime.now().year}-{hash(str(datetime.datetime.now()))}"
    }


###############################################################################
# 4. Utility: calculate contribution room
###############################################################################
@tool(name="calculate_contribution_room", permission=ToolPermission.READ_ONLY)
def calculate_contribution_room(user_id: str, current_limit: float) -> Dict[str, float]:
    """Return the remaining TFSA contribution room."""
    profile = retrieve_user_profile(user_id)
    current_year = datetime.datetime.now().year

    birth_year = current_year - profile["age"]
    first_year = max(profile["first_tfsa_year"], birth_year + 18)

    limits = {
        2019: 6000, 2020: 6000, 2021: 6000, 2022: 6000,
        2023: 6500, 2024: 7000
    }
    total_room = sum(limits.get(y, 6000) for y in range(first_year, current_year))
    total_room += current_limit

    used_room = profile["past_contributions"] + profile["current_year_contributions"]
    available_room = total_room - used_room + profile["withdrawals_last_year"]
    return {
        "available_room": available_room,
        "role": "calculation_agent",
        "content": f"Available contribution room: ${available_room:.2f}"
    }
