import re
from datetime import datetime
from typing import Dict, Annotated

from mcp.server.fastmcp import FastMCP

from e_transfer_assistant import run_etransfer_limit_increase

# ... (Keep all your existing agent code above) ...

# ======================
# 7. FastAPI MCP Server
# ======================
mcp = FastMCP(
    title="E-Transfer Limit Increase MCP Server",
    description="Agentic banking service for handling e-Transfer limit increase requests",
    version="1.0.0"
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
def check_e_transfer_limit(user_id: Annotated[str, "bank user ID"]) -> Dict:
    """Check user's e-Transfer limit?"""
    try:
        result = run_etransfer_limit_increase("What's my e-Transfer limit??", user_id)
        return {
            "current_limit": result.get("current_limit"),
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "error": f"Failed to check current e-transfer limit: {str(e)}",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }


@mcp.tool()
def increase_limit(user_input: Annotated[str, "User input contains a contribution transaction amount"],
                   user_id: Annotated[str, "bank user ID"]) -> Dict:
    """Endpoint for e-Transfer limit increase requests"""
    try:
        # Execute agent workflow
        result = run_etransfer_limit_increase(user_input, user_id)

        # Extract final message
        final_message = result["messages"][-1]["content"] if result.get("messages") else "No response generated"

        # Prepare response
        if result.get("new_limit"):
            success = "✅" in final_message

            return {
                "success": success,
                "user_id": user_id,
                "new_limit": result["new_limit"],
                "response": final_message,
                "transaction_id": re.search(r"LIMIT-\S+", final_message).group() if "LIMIT" in final_message else None,
                "timestamp": datetime.now().isoformat()
            }
        else:
            return {
                "error": result.get("eligibility_reason", "Eligibility check failed"),
                "user_id": user_id,
                "response": final_message,
                "timestamp": datetime.now().isoformat()
            }

    except Exception as e:
        return {
            "error": f"Increase limit failed: {str(e)}",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }


@mcp.resource("etransfer-service://{user_input}/{user_id}")
def handle_etransfer_request(user_input: str, user_id: str = "user_456") -> Dict:
    """Endpoint for handling e-Transfer related requests"""
    print(
        f"[{datetime.now().isoformat()}] Resource called: handle_etransfer_request with parameters: user_input='{user_input}', user_id='{user_id}'")
    try:
        # Execute workflow
        result = run_etransfer_limit_increase(user_input, user_id)

        # Prepare response
        if result.get("new_limit"):
            return {
                "success": True,
                "new_limit": result["new_limit"],
                "response": result["messages"][-1]["content"] if result.get("messages") else "Limit increased"
            }
        else:
            return {
                "success": False,
                "reason": result.get("eligibility_reason", "Eligibility check failed"),
                "response": result["messages"][-1]["content"] if result.get("messages") else "Request failed"
            }
    except Exception as e:
        return {
            "error": f"Request failed: {str(e)}",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat()
        }


if __name__ == "__main__":
    print("Starting e-transfer Assistant MCP Server...")
    # Initialize and run the server
    mcp.run(transport='stdio')
