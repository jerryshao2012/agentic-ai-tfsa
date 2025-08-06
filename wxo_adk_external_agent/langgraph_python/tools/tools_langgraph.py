"""
tools_langgraph.py – TFSA LangGraph Assistant tools for watsonx Orchestrate
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Any

import requests
from ibm_watsonx_orchestrate.agent_builder.tools import tool, ToolPermission

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Configuration – read once, reuse everywhere
# ------------------------------------------------------------------
TFSA_BASE_URL = os.getenv(
    "TFSA_BASE_URL",
    "https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud"
)


# ------------------------------------------------------------------
# Helper: one-liner POST with JSON payload
# ------------------------------------------------------------------
def _post_tfsa(path: str, payload: Dict[str, Any], timeout: int = 90) -> str:
    url = f"{TFSA_BASE_URL.rstrip('/')}{path}"
    logger.info("POST %s with payload %s", url, payload)

    headers = {
        'Content-Type': 'application/json',
        'Accept': 'text/plain'
    }
    query_params = {}
    response = requests.post(
        url,
        headers=headers,
        params=query_params,
        json=payload,
        timeout=timeout
    )
    response.raise_for_status()
    return response.text


# ------------------------------------------------------------------
# TFSA Advice Tool
# ------------------------------------------------------------------
@tool(name="langgraph_get_tfsa_advice",
      permission=ToolPermission.READ_ONLY)
def get_tfsa_advice(user_input: str, user_id: str = None) -> str:
    """
    Ask any TFSA-related question and receive CRA-compliant guidance.

    :param user_input: (str) The user’s question (e.g., "What are the overcontribution penalty policies?")
    :param user_id: (str, Optional) The unique customer identifier

    :returns: Text response from TFSA service
    """
    payload = {"user_input": user_input}
    if user_id:
        payload["user_id"] = user_id
    return _post_tfsa(
        path="/api/v1/get_tfsa_advice",
        payload=payload
    )


if __name__ == "__main__":
    # Synchronous test execution
    import time

    print("=== Policy Question ===\nWhat are the annual dollar limits for each year of TSFA?")

    start_time = time.time()
    advice_response = get_tfsa_advice("What are the annual dollar limits for each year of TSFA?")
    print(advice_response)
    print(
        "\n=== Request finished in %.3f seconds ==="
        % (time.time() - start_time)
    )
