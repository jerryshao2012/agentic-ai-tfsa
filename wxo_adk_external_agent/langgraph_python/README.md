# IBM Watsonx Orchestrate - External Agent for TFSA

For examples of IBM watsonx Orchestrate external agent development, refer to the [IBM watsonx Orchestrate Developer Toolkit - External Agent](https://github.com/watson-developer-cloud/watsonx-orchestrate-developer-toolkit).

For official feature documentation, refer to the [IBM Developer API Catalog](https://developer.ibm.com/apis/catalog/watsonorchestrate--custom-assistants/api/API--watsonorchestrate--ibm-watsonx-orchestrate-api#Register_an_external_chat_completions_agent__agents_external_chat_post).

## Overview

This TFSA implementation demonstrates how to deploy an external agent as a serverless application in IBM Cloud. The application leverages 
[FastAPI](https://fastapi.tiangolo.com) and [LangGraph](https://www.langchain.com/langgraph) to create a chat completion service that integrates with Ollama, Deepseek, IBM watsonx and OpenAI models. It also includes AI tool for TFSA policy search using [Tavily API](https://www.tavily.com).

## Features

- **Chat Completion Service**: The application provides a RESTful API endpoint for chat completions, supporting both synchronous and streaming responses following the specification of IBM Orchestrate external agents.
- **Integration with AI Models**: It provides an example that supports multiple AI models, including local Ollama, Deepseek, IBM's watsonx and OpenAI's GPT, allowing for flexible AI-driven interactions.
- **Tool Integration**: The application includes tool for TFSA policy search using [Tavily API](https://www.tavily.com), which can be invoked during chat interactions.
- **Token Management**: Implements a caching mechanism for IBM Cloud IAM tokens to optimize authentication processes.
- **Logging and Debugging**: Logging is set up to facilitate debugging and monitoring of the application.

## Security Limitations

Please be aware that this example accepts any API Key or Bearer token for authentication. 
It is recommended to implement your own authentication security measures to ensure proper security.

## Deployment Instructions

### Step 1: Create a Code Engine Project

1. **Using IBM Cloud Web UI:**
   - Navigate to [IBM Cloud Code Engine Projects](https://cloud.ibm.com/containers/serverless/projects) and select **Create**. Name your project, for instance `wxo-agent-test1`.
   - Or if project is created, copt the project name `ce-itz-wxo-688a2b3ac1fc751be4edfa`
   - Select the agent you created (`ce-itz-wxo-688a2b3ac1fc751be4edfa`) and choose the **Application** menu item from the left navigation panel.

2. **Create an API Key for Registry Secret:**
   - Select **Manage** from the title bar menu and go to **Access (IAM)**.
   - From the left navigation menu, select **API keys**.
   - Click **Create** and copy the new API key for use in the registry secret.

     3. **Create the Code Engine Application:**
        - Click the **Create** button to start creating an application.
        - Create from source code
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
          - **Application name:** Any name, for instance `wxo-agent-tfsa-app1`
          - **Domain mappings:** Public
          Note: if you get an error "Failed to create namespace: You are not authorized to access the IBM Container Registry in this account", try `podman` command to build image locally and then push to repository.
            ```shell
            brew install podman
            podman machine init
            podman machine start
            # Use #username iamapikey, #pwd <WATSONX_API_KEY> to login
            podman login us.icr.io
            cd wxo_adk_external_agent/langgraph_python
            podman build . -t tfsa-agent-app --platform linux/amd64
            podman tag localhost/tfsa-agent-app us.icr.io/cr-itz-4yv6abja/tfsa-agent-app:latest
            podman push us.icr.io/cr-itz-4yv6abja/tfsa-agent-app:latest
            ```
        - Create from image
          - Under **Code**, select **Use an existing container image**.
          - In the **Image reference** field, enter `private.us.icr.io/cr-itz-4yv6abja/tfsa-agent-app`.

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

5. **Test the Application:**
   - Choose **Test application** and click **Application URL**.
     - It is expected this page will not be found, we need to slightly update the path
     - Append `/docs` to the end of the URL path to view a formatted API page.
       - Example: `https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/docs`
         - Test API:
           - Sync test
           ```shell
            curl -X 'POST' \
              'https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/get_tfsa_advice' \
              -H 'accept: application/json' \
              -H 'Content-Type: application/json' \
              -d '{"user_input": "What are the annual dollar limits for each year of TSFA, including 2025?"}'
           ```
           - Streaming test
           ```shell
             curl -X 'POST' \
                'https://wxo-agent-tfsa-app1.1yhdbkea049z.us-south.codeengine.appdomain.cloud/api/v1/get_tfsa_advice' \
                -H 'Content-Type: application/json' \
                -d '{"user_input": "What are the annual dollar limits for each year of TSFA, including 2025?","stream": true}'
           ```

### Step 2: Deploy tools linked with the External Agent

1. Import tools with its requirements to agent
```shell
orchestrate tools import -k python -r "requirements.txt" -f "tools_langgraph.py"
```

2. Import agents
```shell
orchestrate agents import -f tfsa_langgraph_external_agent.yaml
```

### Step 3: Call the new External Agent from Orchestrate

1. **In IBM watsonx orchestrate Web UI:**
   - From the top left hamburger menu, select **Agent Configuration**.
   - Select **Chat** from the left-hand navigation.
   - Type a question that should route to the new agent, like `What are the annual dollar limits for each year of TSFA including 2025?`
   - The results from the external agent should be streamed to the IBM watsonx Orchestrate chat window

![Alt text](./chat_external_agent.png "Example of a chat to the external agent from IBM watsonx Orchestrate")

Test FAQs
```text
What are the annual dollar limits for each year of TSFA?
What are the annual dollar limits for each year of TSFA including 2025?
What are the overcontribution penalty policies?
What are withdrawal rules?
I want to contribute to my TFSA
My user ID is user_123. What is my contribution room for 2025?
Yes, I want to contribute $2000
```