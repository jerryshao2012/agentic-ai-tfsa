import os
import re
import subprocess
from typing import Tuple, List, Dict

import streamlit as st
from langchain_community.llms import Ollama

# Configuration
MCP_CLIENTS = {
    "TFSA": "python tfsa_mcp_client.py",
    "e-Transfer": "python e_transfer_mcp_client.py"
}

# Initialize a local LLM for client selection
llm = Ollama(model="deepseek-coder:latest")


def classify_query(user_input: str) -> str:
    """Use LLM to classify which service the query belongs to"""
    prompt = f"""
    Classify this banking query into one of these categories:
    - TFSA: Tax-Free Savings Account questions, contribution room, withdrawals
    - e-Transfer: Electronic transfers, sending money, transfer limits

    Query: "{user_input}"

    Respond ONLY with either "TFSA" or "e-Transfer" (no other text)
    """

    try:
        response = llm.invoke(prompt)
        # Clean up the response
        response = response.strip().replace('"', '').replace("'", "")
        if response in ["TFSA", "e-Transfer"]:
            return response
    except:
        pass

    # Fallback to keyword matching if LLM fails
    user_input = user_input.lower()
    tfsa_keywords = ["tfsa", "contribution", "room", "tax-free", "tsfa", "savings account"]
    etransfer_keywords = ["e-transfer", "transfer", "limit", "increase", "send money", "interac"]

    if any(keyword in user_input for keyword in tfsa_keywords):
        return "TFSA"
    elif any(keyword in user_input for keyword in etransfer_keywords):
        return "e-Transfer"
    return "TFSA"  # Default


def run_mcp_client(client: str, user_input: str) -> Tuple[str, str, List[Dict]]:
    """Run MCP client and parse its output"""
    # Pass current environment variables
    env = os.environ.copy()

    # Create a temporary file to capture full output
    output_file = f"{client}_output.txt"
    command = f"{MCP_CLIENTS[client]} --message '{user_input}' > {output_file} 2>&1"

    try:
        subprocess.run(
            command,
            shell=True,
            env=env,
            check=True
        )

        # Read the full output
        with open(output_file, "r") as f:
            response = f.read()
        os.remove(output_file)
    except subprocess.CalledProcessError as e:
        return f"Error: Client process failed with code {e.returncode}", "error", []
    except Exception as e:
        return f"Error: {str(e)}", "error", []

    # Parse output to detect invoked components
    invoked_components = []

    # Detect tool/resource/prompt usage in output
    if "Tool called:" in response:
        matches = re.findall(r"Tool called: (\w+)", response)
        for match in matches:
            invoked_components.append({"type": "tool", "name": match})

    if "Resource called:" in response:
        matches = re.findall(r"Resource called: (\w+)", response)
        for match in matches:
            invoked_components.append({"type": "resource", "name": match})

    if "Prompt called:" in response:
        matches = re.findall(r"Prompt called: (\w+)", response)
        for match in matches:
            invoked_components.append({"type": "prompt", "name": match})

    # Extract the final AI response
    ai_response_match = re.search(r"AI: (.+)", response, re.DOTALL)
    ai_response = ai_response_match.group(1).strip() if ai_response_match else response

    return ai_response, "success", invoked_components


def main():
    st.title("🏦 Banking Assistant MCP Host")
    st.caption("Integrates TFSA and e-Transfer services using MCP architecture")

    # Initialize chat history
    if "messages" not in st.session_state:
        st.session_state.messages = []

    # Display chat messages
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if "components" in message and message["components"]:
                st.caption("Invoked components:")
                for comp in message["components"]:
                    st.markdown(f"- `{comp['type']}: {comp['name']}`")

    # User input
    if prompt := st.chat_input("How can I help with your banking today?"):
        # Add user message to chat history
        st.session_state.messages.append({"role": "user", "content": prompt})

        # Display user message
        with st.chat_message("user"):
            st.markdown(prompt)

        # Classify query using LLM
        with st.spinner("Determining best service for your query..."):
            selected_client = classify_query(prompt)

        # Display client selection
        with st.status(f"Routing to {selected_client} service..."):
            # Get response from MCP client
            response, status, components = run_mcp_client(selected_client, prompt)

            # Add assistant response to chat history
            st.session_state.messages.append({
                "role": "assistant",
                "content": response,
                "client": selected_client,
                "components": components
            })

        # Display assistant response
        with st.chat_message("assistant"):
            st.markdown(response)
            st.caption(f"Service: **{selected_client}**")

            # Only show components section if components exist
            if components:
                st.caption("Invoked components:")
                for comp in components:
                    st.markdown(f"- `{comp['type']}: {comp['name']}`")


if __name__ == "__main__":
    main()
