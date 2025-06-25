import datetime
import operator
import os
from typing import TypedDict, Annotated, Optional

from dotenv import load_dotenv
from langchain.tools import tool
from langgraph.graph import StateGraph, END

load_dotenv('.env')

# Configuration for Deepseek. Initialize DeepSeek LLM: pip install -U langchain-deepseek
# from langchain_deepseek import ChatDeepSeek
#
# DEEPSEEK_API_KEY = os.environ['DEEPSEEK_API_KEY']
# llm = ChatDeepSeek(model="deepseek-chat", temperature=0, api_key=DEEPSEEK_API_KEY)

# Configuration for Ollama. Initialize Ollama with qwen2.5vl:7b model locally
from langchain_ollama import ChatOllama

llm = ChatOllama(
    model="qwen2.5vl:7b",
    # other params...
    temperature=0)  # Use your preferred qwen2.5vl:7b variant


# Configuration for Watsonx.ai
# from ibm_watson_machine_learning.foundation_models import Model
# from ibm_watson_machine_learning.metanames import GenTextParamsMetaNames as GenParams
#
# IBM_CLOUD_URL = os.getenv("IBM_CLOUD_URL")
# WATSONX_API_KEY = os.getenv("API_KEY")
# WATSONX_PROJECT_ID = os.getenv("PROJECT_ID")
#
# # Initialize Watsonx model
# watsonx_params = {
#     GenParams.DECODING_METHOD: "greedy",
#     GenParams.MIN_NEW_TOKENS: 1,
#     GenParams.MAX_NEW_TOKENS: 1024,
#     GenParams.TEMPERATURE: 0,
# }
#
# watsonx_model = Model(
#     model_id="ibm/granite-13b-instruct-v2",
#     params=watsonx_params,
#     credentials={
#         "apikey": WATSONX_API_KEY,
#         "url": IBM_CLOUD_URL
#     },
#     project_id=WATSONX_PROJECT_ID
# )
#
#
# # Helper function for Watsonx invocation
# class WatsonLLM:
#     @staticmethod
#     def invoke(prompt: str) -> str:
#         """Invoke Watsonx model with prompt and return response"""
#         response = watsonx_model.generate_text(prompt)
#         return response
#
#
# llm = WatsonLLM()


# ======================
# 1. State Definition
# ======================
class AgentState(TypedDict):
    user_input: str
    user_id: str
    user_profile: Optional[dict]
    current_limit: Optional[float]
    eligibility_status: Optional[bool]
    eligibility_reason: Optional[str]
    new_limit: Optional[float]
    messages: Annotated[list[dict], operator.add]


# ======================
# 2. Tool Definitions
# ======================
@tool
def retrieve_user_profile(user_id: str) -> dict:
    """Retrieves user's profile from bank database"""
    return {
        "name": "Brian",
        "account_age": 18,  # months
        "account_status": "active",
        "kyc_status": "verified",
        "fraud_flags": 0,
        "avg_balance": 15000.00,
        "current_etransfer_limit": 3000.00
    }


@tool
def check_eligibility(user_id: str) -> dict:
    """Checks if user is eligible for e-Transfer limit increase"""
    profile = retrieve_user_profile.invoke(user_id)

    # Eligibility rules
    eligible = True
    reasons = []

    if profile["account_age"] < 6:
        eligible = False
        reasons.append("Account must be at least 6 months old")

    if profile["account_status"] != "active":
        eligible = False
        reasons.append("Account must be in active status")

    if profile["kyc_status"] != "verified":
        eligible = False
        reasons.append("KYC verification required")

    if profile["fraud_flags"] > 0:
        eligible = False
        reasons.append("Account has fraud flags")

    return {
        "eligible": eligible,
        "reasons": reasons,
        "max_possible_limit": 10000.00  # Based on account profile
    }


@tool
def increase_etransfer_limit(user_id: str, new_limit: float) -> dict:
    """Executes e-Transfer limit increase in core banking system"""
    # In real system, would integrate with core banking API
    return {
        "success": True,
        "new_limit": new_limit,
        "effective_date": datetime.datetime.now().isoformat(),
        "reference_id": f"LIMIT-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    }


# ======================
# 3. Agent Definitions
# ======================
def profile_agent(state: AgentState):
    """Retrieves user profile and current limit"""
    profile = retrieve_user_profile.invoke(state["user_id"])
    return {
        "user_profile": profile,
        "current_limit": profile["current_etransfer_limit"],
        "messages": [{
            "role": "system",
            "content": f"Retrieved profile for {profile['name']} (Current limit: ${profile['current_etransfer_limit']:.2f})"
        }]
    }


def eligibility_agent(state: AgentState):
    """Determines eligibility for limit increase"""
    result = check_eligibility.invoke(state["user_id"])

    # Generate natural language explanation
    prompt = f"""
    <|system|>
    You are a banking assistant explaining eligibility for e-Transfer limit increases.
    Eligibility result: {result['eligible']}
    Reasons: {', '.join(result['reasons']) if result['reasons'] else 'All requirements met'}
    Current limit: ${state['current_limit']:.2f}
    Max possible limit: ${result['max_possible_limit']:.2f}

    Provide a concise 1-2 sentence explanation for the user.
    </s>
    """
    explanation = llm.invoke(prompt)

    return {
        "eligibility_status": result["eligible"],
        "eligibility_reason": explanation,
        "messages": [{
            "role": "eligibility_agent",
            "content": explanation
        }]
    }


def limit_adjustment_agent(state: AgentState):
    """Determines and applies new limit"""
    if not state["eligibility_status"]:
        return {
            "messages": [{
                "role": "assistant",
                "content": "⚠️ Unable to increase limit: " + state["eligibility_reason"]
            }]
        }

    # Calculate new limit (business logic)
    profile = state["user_profile"]
    result = check_eligibility.invoke(state["user_id"])
    new_limit = min(
        result["max_possible_limit"],
        state["current_limit"] * 1.67  # Standard increase to $5k from $3k
    )

    # Execute limit increase
    increase_result = increase_etransfer_limit.invoke({"user_id": state["user_id"], "new_limit": new_limit})

    # Generate confirmation message
    prompt = f"""
    <|system|>
    You are a banking assistant confirming a successful e-Transfer limit increase.
    Details:
    - Previous limit: ${state['current_limit']:.2f}
    - New limit: ${new_limit:.2f}
    - Effective immediately
    - Reference ID: {increase_result['reference_id']}

    Create a friendly confirmation message with emojis.
    </s>
    """
    confirmation = llm.invoke(prompt)

    return {
        "new_limit": new_limit,
        "messages": [{
            "role": "assistant",
            "content": confirmation
        }]
    }


# ======================
# 4. Graph Construction
# ======================
workflow = StateGraph(AgentState)

# Define nodes
workflow.add_node("profile_agent", profile_agent)
workflow.add_node("eligibility_agent", eligibility_agent)
workflow.add_node("limit_adjustment_agent", limit_adjustment_agent)

# Define edges
workflow.set_entry_point("profile_agent")
workflow.add_edge("profile_agent", "eligibility_agent")
workflow.add_edge("eligibility_agent", "limit_adjustment_agent")
workflow.add_edge("limit_adjustment_agent", END)

# Compile the graph
app = workflow.compile()

png_graph = app.get_graph().draw_mermaid_png()
with open("e_transfer_graph.png", "wb") as f:
    f.write(png_graph)

print(f"Graph saved as 'e_transfer_graph.png' in {os.getcwd()}")


# ======================
# 5. Execution Function
# ======================
def run_etransfer_limit_increase(user_input: str, user_id: str = "user_456"):
    """Run the agent workflow for limit increase"""
    state = {
        "user_input": user_input,
        "user_id": user_id,
        "user_profile": None,
        "current_limit": None,
        "eligibility_status": None,
        "eligibility_reason": None,
        "new_limit": None,
        "messages": []
    }

    print(f"\n🔹 USER REQUEST: '{user_input}'")
    accumulated_state = state.copy()
    # Execute workflow
    for step in app.stream(state):
        for node_name, node_output in step.items():
            # Update accumulated state with node value
            accumulated_state.update(node_output)

            # Print node output
            if 'messages' in node_output and node_output['messages']:
                msg = node_output['messages'][-1]
                print(f"🔹 [{node_name.upper()}]: {msg['content']}")

    return accumulated_state


# ======================
# 6. Example Usage
# ======================
if __name__ == "__main__":
    print("===== E-TRANSFER LIMIT INCREASE ASSISTANT =====")
    final_state = run_etransfer_limit_increase("How do I increase my e-Transfer limit?")

    # Display final result
    if final_state.get("new_limit"):
        print(f"\n💎 RESULT: New e-Transfer limit = ${final_state['new_limit']:.2f}")
    elif final_state.get("eligibility_reason"):
        print(f"\n⚠️ RESULT: {final_state['eligibility_reason']}")
