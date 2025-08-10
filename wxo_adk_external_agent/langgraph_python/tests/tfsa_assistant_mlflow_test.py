# tfsa_assistant_mlflow_test.py
import os
import sys

# Add the parent directory to the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import mlflow

from tfsa_assistant import graph_app

# Enabling tracing for LangGraph (LangChain)
mlflow.langchain.autolog()

# Optional: Set a tracking URI and an experiment
mlflow.set_tracking_uri("http://localhost:5000")
mlflow.set_experiment("TFSA LangGraph")

if __name__ == "__main__":
    # Build LangGraph input
    state = {
        "user_input": "What are the annual dollar limits for each year of TSFA, including 2025?",
        "user_profile": None,
        "search_results": None,
        "contribution_room": None,
        "contribution_amount": None,
        "messages": [],
    }
    result = graph_app.invoke(state)
    print(result)
