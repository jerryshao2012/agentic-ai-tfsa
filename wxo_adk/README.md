## ✅ Steps – watsonx Orchestrate

1. Installation
   * Prerequisites
     * **Python**: The programming language that the ADK is written in. The ADK requires at least Python 3.11, and the latest compatible version is Python 3.13. For more information, see [Python](https://www.python.org/downloads/).
     * **Pip**: Pip is Python’s package manager. In some operating systems, it’s included with Python’s installation. For more information, see [Pip](https://pip.pypa.io/en/stable/installation/).
     * Create and activate a virtual environment with venv to install the ADK. For more information, see [venv - Creation of virtual environments](https://docs.python.org/3/library/venv.html).
   * Installing the ADK

     On your local computer open your Terminal / Command Prompt and run the following commands:
     1. For Mac users, install the ADK with pip:
     ```shell
     pip install ibm-watsonx-orchestrate
     ```
     2. For Windows users, you will need to setup a Windows Subsystem for Linux environment. Open PowerShell then run following:
     ```shell
     wsl --install
     sudo apt-get update
     sudo apt install python3-full
     sudo python3 -m venv venv
     source venv/bin/activate
     pip3 install ibm-watsonx-orchestrate
     sudo apt install net-tools
     ```
     3. Check watsonx Orchestrate CLI version
     ```shell
     orchestrate --version
     ```
     After installation, you can start using the ADK and its CLI. For more information on available commands and arguments, use the --help argument at the end of a command. For example: orchestrate --help.
   * Installing dependencies   
   ```shell
   pip install -r requirements.txt
   ```
   
2. Setup remote watsonx Orchestrate

   In order to publish and deploy your agents and tools in this lab you need to connect to your watsonx Orchestrate environment. To do that you will need two credentials: your instance URL and your IBM cloud API key.
   1. Create and download your API key & get the watsonx Orchestrate instance URL
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
   To make sure the orchestrate environment has been successfully created and activated you can run orchestrate env list.
   ```shell
   orchestrate env list
   ```
   You can list all LLMs installed in your watsonx Orchestrate environment:
   ```shell
   orchestrate models list
   ```

2. Deployment steps

   Project structure:
   ```text
   wxo_adk/
   ├── agents/
   │   ├── tfsa_calculation_agent.yaml
   │   ├── tfsa_orchestrator_agent.yaml
   │   ├── tfsa_policy_agent.yaml
   │   ├── tfsa_transaction_agent.yaml
   ├── tools/
   │   ├── requirements.txt
   │   ├── tavily_search.yaml.example
   │   ├── tools.py
   ├── .env.example
   ├── demo.png
   ├── README.md
   ```

   1. Importing connections (Connection in reserved TechZone itz-watsonx-event-006 is not working. Omit this step.)
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

   2. Import tools with its requirements to agent
      
      Connection in reserved TechZone itz-watsonx-event-006 is not working. Avoid using it, use itz-watsonx-event-004 instead:
      ```shell
      orchestrate tools import -k python -r "requirements.txt" -f "tools.py"
      ```
      Note: for backup
      ```shell
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

   3. Import agents
      ```shell
      orchestrate agents import -f tfsa_policy_agent.yaml
      orchestrate agents import -f tfsa_calculation_agent.yaml
      orchestrate agents import -f tfsa_transaction_agent.yaml
      orchestrate agents import -f tfsa_orchestrator_agent.yaml
      ```

    Note: Download existing Agent
    ```shell
    orchestrate agents export -n tfsa_calculation_agent -k external -o tfsa_calculation_agent.yaml --agent-only
    orchestrate agents export -n tfsa_calculation_agent -k external -o tfsa_calculation_agent.zip
    ```
    
    Note: Remove existing Agent
    ```shell
    orchestrate agents remove --name tfsa_orchestrator --kind native
    orchestrate agents remove --name tfsa_transaction_agent --kind native
    orchestrate agents remove --name tfsa_calculation_agent --kind native
    orchestrate agents remove --name tfsa_policy_agent --kind native
    ```

   4. Test in the watsonx Orchestrate chat UI or via the external-chat provider.
      1. Deploy agents
      2. Select agent & test in watsonx Orchestrate chat UI
   
      Test FAQs
      ```text
      What are the annual dollar limits for each year of TSFA including 2025?
      What are the overcontribution penalty policies?
      What are withdrawal rules?
      I want to contribute to my TFSA
      My user ID is user_123. What is my contribution room for 2025?
      Yes, I want to contribute $2000
      ```
