#!/usr/bin/env python3
"""Quick manual test client for the deployed TFSA AgentCore runtime.

Usage (from langgraph_python/):
    ../.venv/bin/python test_agent.py
    ../.venv/bin/python test_agent.py "What is the overcontribution penalty?"
    ../.venv/bin/python test_agent.py "contribute $2000" --thread t1
"""
import argparse
import json
import time

import boto3

REGION = "us-east-1"
ARN = (
    "arn:aws:bedrock-agentcore:us-east-1:668864905269:runtime/"
    "tfsa_langgraph_agentcore-ifaWmi8Klq"
)


def ask(client, prompt, thread_id=None):
    payload = {"prompt": prompt}
    if thread_id:
        payload["thread_id"] = thread_id
    t = time.time()
    r = client.invoke_agent_runtime(
        agentRuntimeArn=ARN, qualifier="DEFAULT", payload=json.dumps(payload)
    )
    raw = r["response"].read() if hasattr(r["response"], "read") else b"".join(r["response"])
    data = json.loads(raw)
    text = data[0] if isinstance(data, list) else data
    print(f"\n>>> {prompt}\n[HTTP {r['statusCode']} in {time.time() - t:.1f}s]\n{text}\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("prompt", nargs="?", default="What are the TFSA withdrawal rules?")
    p.add_argument("--thread", default=None, help="thread_id for multi-turn memory")
    args = p.parse_args()

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    ask(client, args.prompt, args.thread)
