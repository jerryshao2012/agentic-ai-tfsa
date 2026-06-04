# IBM Watsonx Orchestrate - External Agent for TFSA

For examples of IBM watsonx Orchestrate external agent development, refer to the [IBM watsonx Orchestrate Developer Toolkit - External Agent](https://github.com/watson-developer-cloud/watsonx-orchestrate-developer-toolkit).

For official feature documentation, refer to the [IBM Developer API Catalog](https://developer.ibm.com/apis/catalog/watsonorchestrate--custom-assistants/api/API--watsonorchestrate--ibm-watsonx-orchestrate-api#Register_an_external_chat_completions_agent__agents_external_chat_post).

For official watsonx Orchestrate Agent Development Kit (ADK) documentation, refer to the [Creating Agents -> provider: external_chat](https://developer.watson-orchestrate.ibm.com/agents/build_agent#provider%3A-external-chat).

## Overview

This TFSA implementation demonstrates how to deploy an external agent as a serverless application in IBM Cloud. The application leverages 
[FastAPI](https://fastapi.tiangolo.com) and [LangGraph](https://www.langchain.com/langgraph) to create a chat completion service that integrates with Ollama, Deepseek, IBM watsonx and OpenAI models. It also includes AI tool for TFSA policy search using [Tavily API](https://www.tavily.com). The system uses multiple specialized agents working together to provide comprehensive assistance to users regarding their TFSA accounts.

The API is designed to be used with IBM watsonx Orchestrate, but can be used independently as well. It must have an [OpenAI-compatible Assistants API endpoint](https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/docs/). Endpoints **honour `X-IBM-THREAD-ID`** for multi-turn conversations, **stream via SSE** when `stream=true`. Both stream and non-stream must be implemented.

## Features

- **Chat Completion Service**: The application provides a RESTful API endpoint for chat completions, supporting both synchronous and streaming responses following the specification of IBM Orchestrate external agents.
- **Integration with AI Models**: It provides an example that supports multiple AI models, including local Ollama, Deepseek, IBM's watsonx and OpenAI's GPT, allowing for flexible AI-driven interactions.
- **Tool Integration**: The application includes tool for TFSA policy search using [Tavily API](https://www.tavily.com), which can be invoked during chat interactions.
- **Token Management**: Implements a caching mechanism for IBM Cloud IAM tokens to optimize authentication processes.
- **Logging and Debugging**: Logging is set up to facilitate debugging and monitoring of the application.
- **Caching**: Implements caching to avoid duplicate requests
- **Streaming**: Supports streaming responses for better user experience
- **Error Handling**: Comprehensive error handling throughout the workflow
- **MLflow Integration**: Tracks token usage with MLflow
- **Multi-Provider LLM Support**: Works with various LLM providers (Watsonx, OpenAI, Ollama, DeepSeek)

Note:
- In `app.py` that defines the `FastAPI` app object, `selected_tools = [run_tfsa_assistant_sync]` in the `chat_completions` function to enable the tool. Please make sure to update this line to match your tool configuration. the function `chat_completions`. You can choose any Python function for the tool.
- Multiple tools can be added to the `chat_completions` function through `selected_tools`. It relays on calling `create_react_agent` to create an agent graph that calls tools in a loop until a stopping condition is met. This is a simple workflow that treat tools as conections. In the real complex business senarios, you may want to use a more sophisticated workflow.
- `app.py` defines a `chat_completions` function that takes a `request` object as input and returns a `response` object.
- We optimize tools calling to optimize performance: if there were one tool in the selected list `selected_tools`, we can directly call the tool to get the result. In testing, it will save 8 to 10 seconds for each tool call.

## Security Limitations

Please be aware that this example accepts any API Key or Bearer token for authentication. 
It is recommended to implement your own authentication security measures to ensure proper security.

## Key Components

![TFSA Agent Workflow](tfsa_graph.png)

1. State Management

The workflow uses `AgentState` as its central state object, which contains:
* User input and profile information
* Search results
* TFSA contribution room calculations
* Messages exchanged between agents
* Transaction details

3. Specialized Agents

The system has several specialized agents, each with distinct responsibilities:

`profile_agent`
* Retrieves user profile information from the bank database
* Initializes the state with user-specific data like age, residency status, past contributions, etc.

`document_agent`
* Provides knowledge about historical TFSA rules and policies
* Generates responses based on known TFSA regulations
* Determines if real-time search is needed for current information

`search_agent`
* Searches current TFSA policies using external sources (Tavily or DuckDuckGo)
* Extracts current year contribution limits and other policy information
* Handles real-time policy verification

`calculation_agent`
* Calculates available TFSA contribution room based on user profile and policy data
* Takes into account past contributions, withdrawals, and historical limits
* Provides user-specific financial calculations

`transaction_agent`
* Handles TFSA contribution transactions
* Validates contribution amounts against available room
* Executes transfers from checking account to TFSA

`response_agent`
* Synthesizes all information into a coherent, human-readable response
* Formats the final output for the user

4. Workflow Logic

The workflow follows this process:
   1. **Entry Point**: Starts with `profile_agent` to gather user information
   2. **Routing Logic**: Based on user input, the system decides the next steps:
      * For contribution room inquiries → `calculation_agent`
      * For transaction requests → `calculation_agent` then `transaction_agent`
      * For general policy questions → `document_agent`
   3. Conditional Processing:
      * If `document_agent` determines real-time search is needed → `search_agent`
      * After calculations, system may proceed to transaction processing
   4. Final Response: All paths lead to `response_agent` which formats the final output

5. TFSA Limit Management

The system maintains a comprehensive database of TFSA limits:
  * Loads historical limits from `tfsa_limits.json`
  * Automatically searches for and updates missing years' limits
  * Saves updated limits back to the JSON file

## Running on AWS (Amazon Bedrock)

The same agent can run on **Amazon Bedrock** as the LLM provider, either locally or
deployed to **Bedrock AgentCore Runtime**. The core logic in `tfsa_assistant_graph.py`
is shared with the watsonx path — only the provider and the deployment wrapper differ.

AWS-specific files (all at the top level, beside the shared modules):

| File | Purpose |
|------|---------|
| `tfsa_agentcore.py` | Bedrock AgentCore entrypoint (`/invocations` + `/ping` on :8080) |
| `deploy_agentcore.py` | Deploys the agent to Bedrock AgentCore Runtime |
| `test_agent.py` | Invokes the deployed runtime over boto3 |
| `otel_utils.py` | Optional CloudWatch (OTEL) observability |
| `requirements_agentcore.txt` | Container requirements for the runtime |

### Prerequisites
* AWS credentials configured (`aws configure`, env vars, or SSO) with Bedrock access
* Model access enabled for your `BEDROCK_MODEL_ID` in the target region (Bedrock console)
* **Docker** running — required only for the deploy step (it builds the container image)
* Python deps installed:
  ```bash
  pip install 'langchain[aws]' bedrock-agentcore bedrock-agentcore-starter-toolkit boto3
  ```

### Environment Setup
Add the AWS variables to your `.env` (loaded automatically by `config.py`):
```shell
AI_SERVICES_PROVIDER=bedrock
BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0   # cross-region inference profile
AWS_REGION=us-east-1
ROUTER_MODE=supervisor                                         # supervisor (LLM routing) or rules
TAVILY_API_KEY=<tavily_api_key>                                # optional; falls back to DuckDuckGo
```

### 1. Run locally
Starts the AgentCore server on `localhost:8080` (uses your AWS creds to call the Bedrock model — nothing is deployed):
```bash
python tfsa_agentcore.py
```
Invoke it from another terminal:
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"prompt": "What are the TFSA dollar limits for each year including 2025?"}'
```
Optional payload fields: `"thread_id"` (multi-turn memory) and `"stream": true` (SSE deltas).

> **Port 8080 already in use?** A previous run is still bound to it. Free it with
> `lsof -ti tcp:8080 | xargs kill`, or run on another port via `PORT=8081 python tfsa_agentcore.py`.

### 2. Deploy to Bedrock AgentCore Runtime
`deploy_agentcore.py` does not auto-load `.env`, so export it first. The script builds the
image, pushes to ECR, creates/updates the runtime, enables CloudWatch observability, waits
for `READY`, and runs smoke tests:
```bash
set -a; source .env; set +a
python deploy_agentcore.py
```
Tear down the runtime and its ECR repo when finished:
```bash
python deploy_agentcore.py --cleanup
```

### 3. Invoke the deployed runtime
```bash
python test_agent.py "What is the overcontribution penalty?"
python test_agent.py "I want to contribute \$2000" --thread t1   # multi-turn
```

## Deployment Instructions

Reserve the following resources: itz-watsonx-event-004 in https://techzone.ibm.com/
- Dev/Test environment: https://techzone.ibm.com/collection/client-engineering-agentic-ai-labs/journey-devtest-environments
  - Prefer this environment for testing since it has Code Engine
  - Get wataonx Orchestrate instance URL from https://us-south.watson-orchestrate.cloud.ibm.com/settings
  - Create a new watsonx AI project in watsonx.ai Studio. Go to Manage to get project id and associate watsonx.ai Runtime service
- or Workshop environment: https://techzone.ibm.com/collection/client-engineering-agentic-ai-labs/journey-workshop-environments

### Automated Deployment with deploy.sh
We provide a deployment script `deploy.sh` that automates the entire deployment process. The script handles authentication, resource setup, container image building, application deployment, and testing.

**Key Features of the Deployment Script:**
*   **Dynamic Agent Discovery**: The script automatically scans the `agents/` directory for all `*.yaml` files. There's no need to maintain a manual list of agents in the script.
*   **Dependency-Aware Deployment**: It analyzes dependencies between agents by checking the `collaborators` field in the YAML files. It ensures that base agents are imported and deployed *before* the agents that depend on them. Cleanup is performed in the reverse order.

### Prerequisites
* IBM Cloud CLI installed and configured
* Required tools: `jq`, `curl`, `podman` (or `docker`), `yq`
* Environment variables set in `.env` file (see example below)

#### Environment Setup
Create a `.env` file with required variables:
```shell
LOGGING_LEVEL=DEBUG

AI_SERVICES_PROVIDER=watsonxai
DEEPSEEK_API_KEY=<deepseek_api_key>

WO_DEVELOPER_EDITION_SOURCE=orchestrate
WO_INSTANCE=<service_instance_url>
WO_API_KEY=<wxo_api_key>

WATSONX_URL=https://us-south.ml.cloud.ibm.com
WATSONX_API_KEY=<watsonx_api_key>
WATSONX_PROJECT_ID=<watsonx_project_id>
WATSONX_SPACE_ID=<watsonx_space_id>

OPENAI_API_KEY=<wxo_api_key>

TAVILY_API_KEY=<tavily_api_key>
```

#### Deployment
1. Make the script executable:
```shell
chmod +x deploy.sh
```
2. Run full deployment:
```shell
./deploy.sh
```
This command runs all steps from authentication to deploying the application and setting up the Orchestrate environment. It automatically discovers, imports, and deploys agents in the correct dependency order.

3. For dry-run (simulation only):
```shell
./deploy.sh --dry-run
```

4. Run specific functions individually:
The script is composed of several functions that can be run independently.
```shell
# Authenticate to IBM Cloud
./deploy.sh authenticate_to_ibmcloud

# Build and push the container image
./deploy.sh build_and_push_image

# Deploy the application to Code Engine
./deploy.sh deploy_application

# Set up the Orchestrate environment, then import and deploy agents
./deploy.sh setup_orchestrate

# Import agents in dependency order
./deploy.sh import_agents

# Deploy agents in dependency order
./deploy.sh deploy_agents

# Undeploy agents in reverse dependency order
./deploy.sh undeploy_agents

# Remove agents from orchestrate
./deploy.sh remove_agents
```
For a full list of functions, run `./deploy.sh --help`.

5. Clean up resources:
```shell
./deploy.sh cleanup_resources
```
This command cleans up all created resources, including the Code Engine application, registry secret, and Orchestrate environment. It also undeploys and removes the agents before cleanup.

### Manual Deployment
#### Step 1: Create a Code Engine Project

1. **Using IBM Cloud Web UI:**
   - Navigate to [IBM Cloud Code Engine Projects](https://cloud.ibm.com/containers/serverless/projects) and select **Create**. Name your project, for instance `wxo-agent-app-test1`.
   - Or if project is created, copy the project name `ce-itz-wxo-688a2b3ac1fc751be4edfa`
   - Select the agent you created (`ce-itz-wxo-688a2b3ac1fc751be4edfa`) and choose the **Application** menu item from the left navigation panel.
   
   - Note: below are the commands to get the project name and resource group name
   ```shell
      export CE_REGION="us-south"
      export IBMCLOUD_API_KEY=$WATSONX_API_KEY
      ibmcloud login --apikey "$IBMCLOUD_API_KEY" -r "$CE_REGION"
      export CE_RESOURCE_GROUP="$(ibmcloud resource groups --output json | jq -r '.[].name' | grep '^itz-')"
      echo "CE_RESOURCE_GROUP=$CE_RESOURCE_GROUP"
      ibmcloud target -g "$CE_RESOURCE_GROUP"
      export CE_PROJECT_ID="$(ibmcloud ce project list --output json | jq -r '.[].name')"
      echo "CE_PROJECT_ID=$CE_PROJECT_ID"
      ibmcloud ce project select -n $CE_PROJECT_ID
   ```

2. **Create an API Key for Registry Secret:**
   - Select **Manage** from the title bar menu and go to **Access (IAM)**.
   - From the left navigation menu, select **API keys**.
   - Click **Create** and copy the new API key for use in the registry secret.
   ```shell
      # Here are equvalent commands
      ibmcloud ce registry create --name tfsa-agent-app-secret \
        --server us.icr.io --username iamapikey --password $IBMCLOUD_API_KEY
      export CONTAINER_NAMESPACE=$(ibmcloud cr namespaces --output json | jq -r '.[].name')
      echo "CONTAINER_NAMESPACE=$CONTAINER_NAMESPACE"
   ```

3. **Create the Code Engine Application:**
   - Deploy from source code
     - Click the **Create** button to start creating an application.
     - Under **Code**, select **Build container image from source code**.
     - In the **Code repo URL** field, enter `https://github.com/jerryshao2012/agentic-ai-tfsa`.
     - Click **Specify build details**:
       - **SSH secret:** None
       - **Branch name:** main
       - **Context directory:** `wxo_adk_external_agent/langgraph_python`
       - Click **Next**
       - **Dockerfile:** Dockerfile (leave default)
       - Click **Next**
       - Under **Registry secret**, create a secret (if one doesn't exist) using the **API Key** created above
         - Or use command to create a registry secret (for container registry access) using:
         ```shell
            ibmcloud plugin update container-registry
            ibmcloud ce registry create --name tfsa-agent-app-secret \
              --server us.icr.io --username iamapikey --password $IBMCLOUD_API_KEY
            export CONTAINER_NAMESPACE=$(ibmcloud cr namespaces --output json | jq -r '.[].name')
            echo "CONTAINER_NAMESPACE=$CONTAINER_NAMESPACE"
         ```
         Replace <api-key> with your details (e.g., IBM Cloud Container Registry server like us.icr.io).

     - **Application name:** Any name, for instance `wxo-agent-tfsa-app1`
     - **Domain mappings:** Public 
     Note: if you get an error "Failed to create namespace: You are not authorized to access the IBM Container Registry in this account", try `podman` command to build image locally and then push to repository. Thanks [@Chung Zheng](mailto:Chung.Zheng@ibm.com) provided the solution.
     ```shell
        brew install yq
        brew install podman
        podman machine init
        podman machine start
        # Use #username iamapikey, #pwd <WATSONX_API_KEY> to login
        podman login us.icr.io
        # Build and Push Container Image
        cd wxo_adk_external_agent/langgraph_python
        podman machine start
        podman build . -t tfsa-agent-app --platform linux/amd64
        podman tag localhost/tfsa-agent-app us.icr.io/$CONTAINER_NAMESPACE/tfsa-agent-app:latest
        podman push us.icr.io/$CONTAINER_NAMESPACE/tfsa-agent-app:latest
     ```
   - Deploy from image
     - Under **Code**, select **Use an existing container image**.
     - In the **Image reference** field, enter `private.us.icr.io/cr-itz-4yv6abja/tfsa-agent-app`.
     - Here is the command to create the application:
     ```shell
        ibmcloud ce application create --name wxo-agent-tfsa-app1 \
          --image private.us.icr.io/cr-itz-4yv6abja/tfsa-agent-app \
          --registry-secret tfsa-agent-app-secret \
          --env LOGGING_LEVEL="$LOGGING_LEVEL" \
          --env AI_SERVICES_PROVIDER="$AI_SERVICES_PROVIDER" \
          --env DEEPSEEK_API_KEY="$DEEPSEEK_API_KEY" \
          --env WO_DEVELOPER_EDITION_SOURCE="orchestrate" \
          --env WO_INSTANCE="$WO_INSTANCE" \
          --env WO_API_KEY="$WO_API_KEY" \
          --env WATSONX_URL="$WATSONX_URL" \
          --env WATSONX_API_KEY="$WATSONX_API_KEY" \
          --env WATSONX_PROJECT_ID="$WATSONX_PROJECT_ID" \
          --env WATSONX_SECRET_ID="$WATSONX_SECRET_ID" \
          --env OPENAI_API_KEY="$OPENAI_API_KEY" \
          --env TAVILY_API_KEY="$TAVILY_API_KEY" \
          --visibility public \
          --port 8080
     ```

4. **Set Environment Variables:**
   - Add the following environment variables:
     - `LOGGING_LEVEL`
     - `AI_SERVICES_PROVIDER`
     - `WATSONX_SPACE_ID` or `WATSONX_PROJECT_ID`
     - `WATSONX_API_KEY`
     - `WATSONX_URL`
     - `TAVILY_API_KEY`
     - `DEEPSEEK_API_KEY` (only needed if you plan to use deepseek models)
     - `OPENAI_API_KEY` (only needed if you plan to use OpenAI models)
   - Select the `Create` button
   - Note:
     - Wait for deployment to complete (it may take a few minutes):
     ```shell
        # Check deployment status
        ibmcloud ce application events --name wxo-agent-tfsa-app1
    
        # Or watch the status until it's ready
        watch -n 5 'ibmcloud ce application get --name wxo-agent-tfsa-app1 | grep "Status"'
     ```
     - Check application details:
     ```shell
        ibmcloud ce application get --name wxo-agent-tfsa-app1
     ```
     - View application logs:
     ```shell
        ibmcloud ce application logs --name wxo-agent-tfsa-app1
     ```
     - Update your application:
     ```shell
        ibmcloud ce application update --name wxo-agent-tfsa-app1 --env NEW_VARIABLE=value
     ```

5. **Test the Application:**
   - Choose **Test application** and click **Application URL**.
     - It is expected this page will not be found, we need to slightly update the path
     - Append `/docs` to the end of the URL path to view a formatted API page.
       - Get public access url:
         ```shell
            export PUBLIC_URL="$(ibmcloud ce application get --name wxo-agent-tfsa-app1 --output url)"
            echo "PUBLIC_URL=$PUBLIC_URL"
         ``` 
       - Example: `https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/chat/completions`
         - Test API:
           - Sync test
           ```shell
             curl -X 'POST' \
              "$PUBLIC_URL/api/v1/chat/completions" \
              -H 'accept: application/json' \
              -H 'Authorization: Bearer xxx' \
              -H 'Content-Type: application/json' \
              -d '{
              "model": "meta-llama/llama-3-2-90b-vision-instruct",
              "messages": [
                {
                  "role": "user",
                  "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
                }
              ],
              "stream": false
            }'
           ```
           - Streaming test
           ```shell
             curl -X 'POST' \
              "$PUBLIC_URL/api/v1/chat/completions" \
              -H 'accept: application/json' \
              -H 'Authorization: Bearer xxx' \
              -H 'Content-Type: application/json' \
              -d '{
              "model": "meta-llama/llama-3-2-90b-vision-instruct",
              "messages": [
                {
                  "role": "user",
                  "content": "What are the annual dollar limits for each year of TSFA, including 2025?"
                }
              ],
              "stream": true
            }'
           ```
   - [MLflow Tracing](https://mlflow.org/docs/latest/genai/tracing/) provides automatic tracing capability for LangGraph, as a extension of its LangChain integration. By enabling auto-tracing for LangChain by calling the `mlflow.langchain.autolog()` function, MLflow will automatically capture the graph execution into a trace and log it to the active MLflow Experiment.
     1. Install the mlflow package
       ```shell
          pip install mlflow
       ```
     2. Launch the MLflow server
       ```shell
          mlflow ui --host 0.0.0.0 --port 5000
       ```
       Note: MLflow UI is available at http://localhost:5000. To terminate the server, run the following command:
       ```shell
          ps -A | grep gunicorn
          pkill -f gunicorn
       ```
     3. Run `tfsa_assistant_mlflow_test.py`:
       ```shell
          python tfsa_assistant_mlflow_test.py
       ```
       ![mlflow LangGraph Tracingl](screenshots/mlflow_langgraph_tracing.png "Example of mlflow Langgraph Tracing")
   - TFSA Chat APIs are integrated with MLflow, which can be viewed in the MLflow UI. We update Dockerfile to expose the MLflow UI.
     - Here is the URL for access:https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/
     ![Example of mlflow Langgraph Tracing](screenshots/chat_mlflow_langgraph_tracing.png "Example of mlflow Langgraph Tracing")
     
#### Step 2: Deploy tools linked with the External Agent

1. Import external agents
Update `api_url` as needed in `tfsa_langgraph_external_agent.yaml`.
- Example: `https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/chat/completions`
```shell
orchestrate env activate watsonx-challenge --api-key <api_key>
orchestrate agents import -f tfsa_langgraph_external_agent.yaml
```

2. Import native agents 

This native agent is used to route the question to the external agent. Only native agents can be listed in watsonx Orchestrate chat UI aoo. 
```shell
orchestrate agents import -f connection_with_tfsa_external_agent.yaml
```

3. Deploy agents in watsonx Orchestrate UI

#### Step 3: Call the new External Agent from Orchestrate

1. **In IBM watsonx orchestrate Web UI:**
   - From the top left hamburger menu, select **Chat** from the left-hand navigation.
   - In the dropdown box, select `Connect to TFSA External Agent`
   - Type a question that should route to the new agent, like `What are the annual dollar limits for each year of TSFA, including 2025?`
   - The results from the external agent should be streamed to the IBM watsonx Orchestrate chat window

![Chat External Agent](screenshots/chat_external_agent.png "Example of a chat to the external agent from IBM watsonx Orchestrate")

Test FAQs
```text
What are the annual dollar limits for each year of TSFA?
What are the annual dollar limits for each year of TSFA, including 2025?
What are the overcontribution penalty policies?
What are withdrawal rules?
I want to contribute to my TFSA
My user ID is user_123. What is my contribution room for 2025?
Yes, I want to contribute $2000
```

2. To check agents' communication logs
    - From Chat UI, select **Manage agents**
    - Click right side button named **View all**
   ![Build agents and tools->View all](screenshots/build_agents_and_tools_view_all.png "Example of View all from IBM watsonx Orchestrate")
    - Click **Connect to TFSA External Agent**
   ![Connect to TFSA External Agent->Trace Detail](screenshots/connect_to_tfsa_external_agent_trace_detail.png "Example of Trace Detail from IBM watsonx Orchestrate")

3. Enhaced agentic communication cache and logs
    - To check agent access request logs from watsonx Orchestrate: https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/logs
   ![Access Logs](screenshots/chat_langgraph_access_log.png "Access Logs")
    - To manage agentic communication cache: https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/cache
   ![Manage Cache](screenshots/chat_langgraph_manage_cache.png "Manage Cache")

Note:
- One issue we are facing in the testing that the external agent calling may take a long time to respond. The configuraion of the external agent call can not be defined in the agent.yaml file. In this sample, we use a self-expired `cache` to cache the external agent call response. This is a workaround to avoid the long waiting time. In production, we should use a persistent distributed `cache` to go around it. ALso only the TFSA policy type user inqueries are cached.
- The chat API call is stateless. To maintain the state, we use a cache with `thread_id` to store the conversation history within session.
- Medium Technical Blog: [Developing Intelligent Agents with IBM watsonx Orchestrate](https://medium.com/@jerry.shao/developing-intelligent-agents-with-ibm-watsonx-orchestrate-cd027c0f8d6b)
- Medium Technical Blog: [Beyond Basics: Developing Advanced External Agents with IBM watsonx Orchestrate](https://medium.com/@jerry.shao/beyond-basics-developing-advanced-external-agents-with-ibm-watsonx-orchestrate-18db983796b7)