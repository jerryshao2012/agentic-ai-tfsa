"""
tools.py – TFSA LangGraph Assistant tools for watsonx Orchestrate
"""
from __future__ import annotations

import logging
import os
from typing import Dict, Any

import httpx
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
async def _post_tfsa(path: str, payload: Dict[str, Any], timeout: int = 45) -> str:
    url = f"{TFSA_BASE_URL.rstrip('/')}{path}"
    logger.info("POST %s with payload %s", url, payload)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        return resp.text


# ------------------------------------------------------------------
# TFSA Advice Tool
# ------------------------------------------------------------------
@tool(name="langgraph_get_tfsa_advice",
      permission=ToolPermission.READ_ONLY)
async def get_tfsa_advice(user_input: str, user_id: str = None) -> str:
    """
    Ask any TFSA-related question and receive CRA-compliant guidance.

    :param user_input: (str) The user’s question (e.g., "What are the overcontribution penalty policies?")
    :param user_id: (str, Optional) The unique customer identifier

    :returns: Text response from TFSA service
    """
    payload = {"user_input": user_input}
    if user_id:
        payload["user_id"] = user_id
    return await _post_tfsa(
        path="/api/v1/get_tfsa_advice",
        payload=payload
    )


if __name__ == "__main__":
    import asyncio
    import time


    async def main():
        print("=== Policy Question ===\nWhat are the annual dollar limits for each year of TSFA, including 2025?")
        start_time = time.time()
        response = await get_tfsa_advice("What are the annual dollar limits for each year of TSFA, including 2025?")
        print(response)
        print(
            "\n=== Request finished in %.3f seconds ==="
            % (time.time() - start_time)
        )


    asyncio.run(main())
