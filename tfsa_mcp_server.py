import re
from datetime import datetime
from typing import Dict, Annotated

# Initialize FastMCP with API metadata
from mcp.server.fastmcp import FastMCP

from tfsa_assistant import run_tfsa_assistant

# Initialize FastMCP with API metadata
mcp = FastMCP(
    "TFSA Assistant API",
    description="Real-time TFSA Contribution Advisor with CRA Compliance",
    version="1.2.0"
)


# ==============================================
# resources, tools, and prompts to be added here
#
#       <to be added in next few sections>
#
# ==============================================

# ======
# Tools
# ======
@mcp.tool()
def check_contribution_room(user_id: Annotated[str, "bank user ID"]) -> Dict:
    """Check user's available TFSA contribution room"""
    try:
        result = run_tfsa_assistant("What's my contribution room?", user_id)
        return {
            "contribution_room": result.get("contribution_room"),
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "error": f"Failed to check contribution room: {str(e)}",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }


@mcp.tool()
def execute_contribution(user_input: Annotated[str, "User input contains a contribution transaction amount"],
                         user_id: Annotated[str, "bank user ID"]) -> Dict:
    """Execute TFSA contribution transaction with an amount"""
    try:
        result = run_tfsa_assistant(user_input, user_id)

        # Extract transaction ID from response
        transaction_id = None
        response = next(
            (msg['content'] for msg in reversed(result['messages'])
             if msg.get('role') == 'assistant'),
            ""
        )
        if match := re.search(r"Transaction ID: (\S+)", response):
            transaction_id = match.group(1)

        success = "✅" in response

        return {
            "success": success,
            "transaction_id": transaction_id,
            "new_contribution_room": result.get("contribution_room"),
            "user_id": user_id,
            "response": response,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "error": f"Contribution failed: {str(e)}",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }


# =======
# Prompt
# =======
@mcp.prompt()
def explain_tfsa_rules(query: str) -> str:
    """Explain TFSA rules in simple terms"""
    return f"""
    As a financial educator, explain TFSA rules focusing on:
    - Contribution limits
    - Withdrawal rules
    - Tax implications

    Structure:
    1. Start with 1-sentence summary
    2. Break into bullet points
    3. End with practical example

    Use simple language (grade 8 level).

    Specific query: {query}
    """


# ==========
# Resources
# ==========
@mcp.resource("tfsa-advice://{user_input}/{user_id}")
def get_tfsa_advice(user_input: str, user_id: str = "user_123") -> Dict:
    """
        As a certified TFSA specialist, respond to user queries using these guidelines:
        1. Verify contribution room before suggesting amounts
        2. Mention penalty risks for over-contributions
        3. Provide current year's contribution limit
        4. Explain withdrawal recontribution rules
        5. Always include transaction ID when applicable

        Current Date: {current_date}
        User ID: {user_id}
        Query: {user_input}
    """
    try:
        # Execute workflow
        result = run_tfsa_assistant(user_input, user_id)

        # Extract final assistant response
        response = next(
            (msg['content'] for msg in reversed(result['messages'])
             if msg.get('role') == 'assistant'),
            "No response generated"
        )

        # Prepare output
        output = {
            "response": response,
            "contribution_room": result.get("contribution_room"),
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }

        # Extract transaction details if available
        if transaction_match := re.search(r"Transaction ID: (\S+)", response):
            output["transaction_id"] = transaction_match.group(1)

        if "contribution_amount" in result:
            output["contribution_amount"] = result["contribution_amount"]

        return output

    except Exception as e:
        return {
            "response": f"❌ Processing error: {str(e)}",
            "error": True,
            "error_details": str(e),
            "timestamp": datetime.now().isoformat()
        }


@mcp.resource("tfsa-annual://limit")
def get_tfsa_annual_dollar_limit() -> str:
    """The annual Tax-Free Savings Account (TFSA) dollar limit for each of the years from 2009 to 2025"""
    return """
        Annual limit for 2009-2012: $5000
        Annual limit for 2013-2014: $5500
        Annual limit for 2015: $10000
        Annual limit for 2016-2018: $5500
        Annual limit for 2019-2022: $6000
        Annual limit for 2023: $6500
        Annual limit for 2024 and 2025: $7000
    """


if __name__ == "__main__":
    print("Starting TFSA Assistant MCP Server...")
    # Initialize and run the server
    mcp.run(transport='stdio')
