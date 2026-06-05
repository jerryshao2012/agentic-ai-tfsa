#!/usr/bin/env python3
# deploy_agentcore.py
"""
Deploy the TFSA LangGraph agent to Amazon Bedrock AgentCore Runtime using the
bedrock-agentcore starter toolkit. Run from inside the `langgraph_python/` directory.

Prerequisites:
  * Docker running locally (the starter toolkit builds the container image).
  * AWS credentials configured (e.g. `aws configure` / env vars / SSO).
  * Model access enabled for BEDROCK_MODEL_ID in the target region (Bedrock console).

Observability (CloudWatch GenAI Observability):
  * requirements_agentcore.txt includes aws-opentelemetry-distro and this script sets
    AGENT_OBSERVABILITY_ENABLED=true, so the runtime exports traces/metrics to CloudWatch.
  * CloudWatch Transaction Search must be enabled once per account/region; this script
    attempts it automatically and prints manual steps if it lacks permission.
  * Traces appear under CloudWatch -> GenAI Observability -> Bedrock AgentCore.

Usage:
  export TAVILY_API_KEY=...                 # used by search_agent / TFSA limit lookups
  export BEDROCK_MODEL_ID=amazon.nova-lite-v1:0   # optional override
  python deploy_agentcore.py                # configure + launch + wait + smoke-test
  python deploy_agentcore.py --cleanup      # delete the runtime and its ECR repo
"""
import argparse
import os
import time

import boto3
from boto3.session import Session
from bedrock_agentcore_starter_toolkit import Runtime

AGENT_NAME = "tfsa_langgraph_agentcore"
# Claude Haiku 4.5 via cross-region inference profile (the "us." prefix is required
# for on-demand use). Nova Lite was too weak to faithfully reproduce the TFSA limit
# figures; Haiku transcribes the grounded table reliably.
DEFAULT_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
END_STATUSES = {"READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"}


def enable_transaction_search(region):
    """Best-effort: enable CloudWatch Transaction Search so OTEL spans are ingested.

    Routes X-Ray segments to CloudWatch Logs and indexes them. Requires one-time
    setup per account/region. Prints manual steps if the caller lacks permission.
    """
    try:
        xray = boto3.client("xray", region_name=region)
        dest = xray.get_trace_segment_destination()
        if dest.get("Destination") == "CloudWatchLogs" and dest.get("Status") == "ACTIVE":
            print("Transaction Search already enabled.")
            return
        xray.update_trace_segment_destination(Destination="CloudWatchLogs")
        try:
            xray.update_indexing_rule(
                Name="Default",
                Rule={"Probabilistic": {"DesiredSamplingPercentage": 100}},
            )
        except Exception:
            pass  # indexing rule is optional; destination is the key part
        print("Enabled CloudWatch Transaction Search (X-Ray -> CloudWatch Logs).")
    except Exception as e:
        print(f"Could not auto-enable Transaction Search ({e}).")
        print("  Enable it manually: CloudWatch console -> Settings -> Transaction Search,")
        print("  or `aws xray update-trace-segment-destination --destination CloudWatchLogs`.")


def deploy():
    region = Session().region_name or os.getenv("AWS_REGION", "us-east-1")
    print(f"Deploying '{AGENT_NAME}' to region {region}")

    runtime = Runtime()
    runtime.configure(
        entrypoint="tfsa_agentcore.py",
        auto_create_execution_role=True,
        auto_create_ecr=True,
        requirements_file="requirements_agentcore.txt",
        region=region,
        agent_name=AGENT_NAME,
    )

    env_vars = {
        "AI_SERVICES_PROVIDER": "bedrock",
        "BEDROCK_MODEL_ID": os.getenv("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        "AWS_REGION": region,
        # 'supervisor' = LLM decides routing (more agentic); 'rules' = fast regex router.
        "ROUTER_MODE": os.getenv("ROUTER_MODE", "supervisor"),
        # Export traces/metrics to CloudWatch GenAI Observability via ADOT.
        "AGENT_OBSERVABILITY_ENABLED": "true",
        # The container's working dir (/app) is read-only; the pickle cache must
        # live somewhere writable. /tmp is writable in the AgentCore microVM.
        "CACHE_PATH": "/tmp/cache",
    }
    if os.getenv("TAVILY_API_KEY"):
        env_vars["TAVILY_API_KEY"] = os.environ["TAVILY_API_KEY"]
    else:
        print("WARNING: TAVILY_API_KEY not set — search_agent will fall back to DuckDuckGo.")

    # Synthetic data sources (S3). Forward if a bucket is configured; the agent reads
    # profiles/limits/transactions from here, falling back to the built-in mock if the
    # execution role lacks s3:GetObject (so this is safe to deploy before the IAM grant).
    if os.getenv("DATA_S3_BUCKET"):
        for var in ("DATA_S3_BUCKET", "PROFILE_S3_PREFIX", "TRANSACTIONS_S3_PREFIX",
                    "LIMITS_S3_KEY", "DATA_S3_REGION"):
            if os.getenv(var):
                env_vars[var] = os.environ[var]
        print(f"Data source: s3://{os.environ['DATA_S3_BUCKET']} "
              "(falls back to mock until the execution role has s3:GetObject).")
    else:
        print("DATA_S3_BUCKET not set — agent will use the built-in mock profile.")

    # One-time observability setup so spans are actually ingested.
    enable_transaction_search(region)

    launch_result = runtime.launch(env_vars=env_vars)
    print(f"Launched. agent_id={launch_result.agent_id} arn={launch_result.agent_arn}")

    # Wait until the endpoint is READY
    status = runtime.status().endpoint["status"]
    while status not in END_STATUSES:
        time.sleep(10)
        status = runtime.status().endpoint["status"]
        print(f"  status: {status}")
    print(f"Final status: {status}")

    if status == "READY":
        print("\nSmoke test (sync):")
        resp = runtime.invoke({
            "prompt": "What are the annual dollar limits for each year of TFSA, including 2025?"
        })
        print(resp)

        print("\nSmoke test (multi-turn, thread_id=t1):")
        print(runtime.invoke({
            "prompt": "My user ID is user_123. What is my contribution room for 2025?",
            "thread_id": "t1",
        }))
        print(runtime.invoke({
            "prompt": "Yes, I want to contribute $2000",
            "thread_id": "t1",
        }))

        # Where to find telemetry
        agent_id = launch_result.agent_id
        log_group = f"/aws/bedrock-agentcore/runtimes/{agent_id}-DEFAULT"
        print("\n--- Telemetry ---")
        print(f"CloudWatch log group: {log_group}")
        print("GenAI Observability:  "
              f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}"
              "#gen-ai-observability:agent-core")
        print("Traces (X-Ray):       "
              f"https://{region}.console.aws.amazon.com/cloudwatch/home?region={region}#xray:traces/query")
        print("Tip: spans take a minute to appear; each invoke creates a trace with one "
              "span per agent (supervisor/document/search/calculation/transaction/response) "
              "plus Bedrock token & latency spans.")

    return launch_result, region


def cleanup():
    region = Session().region_name or os.getenv("AWS_REGION", "us-east-1")
    runtime = Runtime()
    # Re-load the existing configuration for this agent without rebuilding.
    runtime.configure(
        entrypoint="tfsa_agentcore.py",
        requirements_file="requirements_agentcore.txt",
        region=region,
        agent_name=AGENT_NAME,
    )
    status_resp = runtime.status()
    agent_id = status_resp.agent_id
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    try:
        control.delete_agent_runtime(agentRuntimeId=agent_id)
        print(f"Deleted runtime {agent_id}")
    except Exception as e:
        print(f"Failed to delete runtime: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cleanup", action="store_true", help="Delete the deployed runtime")
    args = parser.parse_args()
    if args.cleanup:
        cleanup()
    else:
        deploy()
