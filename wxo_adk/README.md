## ✅ Steps – watsonx Orchestrate

1. Install dependencies
   ```bash
   pip install -r requirements.txt
   ```
   1.1 Check watsonx Orchestrate CLI version
    ```bash
    orchestrate --version
    ```
   
2. Setup remote watsonx Orchestrate
   1. Using your watsonx Orchestrate account
      1. [Log in](https://www.ibm.com/docs/en/watsonx/watson-orchestrate/current?topic=orchestrate-logging-in-watsonx) to your watsonx Orchestrate account.
      2. Click your user profile and open the **Settings** page.
      3. Open the **API details** tab and click **Generate API key**.
      4. Copy your Service Instance URL:
      ```html
      https://api.<region>.watson-orchestrate.ibm.com/instances/<wxo_instance_id>
      ```
      5. Create a .env file with the following contents:
      ```properties
      WO_DEVELOPER_EDITION_SOURCE=orchestrate
      WO_INSTANCE=<service_instance_url>
      WO_API_KEY=<wxo_api_key>
      ```
      Replace <service_instance_url>, <wxo_api_key>, <your_wxo_email>, and <your_wxo_password> with the appropriate information.

   2. Activate a remote watsonx Orchestrate environment
   
   To activate a remote the watsonx Orchestrate environment with the ADK, run the following command in the CLI:
   ```shell
   orchestrate env add -n watsonx-challenge -u <service_instance_url> --type ibm_iam --activate
   ```

2. Importing connections
   1. Create a connection yaml file:
   ```yaml
   spec_version: v1
   kind: connection
   app_id: tavily_search
   environments:
       draft:
           kind: api_key
           type: team
           api_key: <tavily_search_api_key>
           server_url: https://nan.com/
       live:
         kind: api_key
         type: team
         api_key: <tavily_search_api_key>
         server_url: https://nan.com/
   ```
   2. Import the connection 
   ```shell
   orchestrate connections import --file tavily_search.yaml
   ```

3. Import tools with its requirements to agent
   ```bash
   orchestrate tools import -k python -r "requirements.txt" -f "tools.py" --app-id tavily_search
   ```
   
   Note: use below command to renew a token to access watsonx Orchestrate
   ```shell
   orchestrate env activate watsonx-challenge --api-key <your_api_key>
   ```
   Note: Remove tools
   ```shell
   orchestrate tools remove -n search_cra_tfsa_policy
   ```

4. Import agents
   ```bash
   orchestrate agents import -f tfsa_policy_agent.yaml
   orchestrate agents import -f tfsa_calculation_agent.yaml
   orchestrate agents import -f tfsa_transaction_agent.yaml
   orchestrate agents import -f tfsa_orchestrator_agent.yaml
   ```

Note: Download existing Agent
```bash
orchestrate agents export -n tfsa_calculation_agent -k external -o tfsa_calculation_agent.yaml --agent-only
orchestrate agents export -n tfsa_calculation_agent -k external -o tfsa_calculation_agent.zip
```

Note: Remove existing Agent
```bash
orchestrate agents remove --name tfsa_orchestrator --kind native
orchestrate agents remove --name tfsa_transaction_agent --kind native
orchestrate agents remove --name tfsa_calculation_agent --kind native
orchestrate agents remove --name tfsa_policy_agent --kind native
```

5. Test in the watsonx Orchestrate chat UI or via the external-chat provider.
   1. Deploy agents
   2. Select agent & test in watsonx Orchestrate chat UI
   
   Test FAQs
   ```text
   What are the annual dollar limits for each year of TSFA?
   What are the overcontribution penalty policies?
   What are withdrawal rules?
   I want to contribute to my TFSA
   My user ID is user_123. What is my contribution room for 2025?
   Yes, I want to contribute $2000
   ```
