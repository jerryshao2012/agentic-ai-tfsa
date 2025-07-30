source .env

orchestrate env add -n watsonx-challenge -u $WO_INSTANCE --type ibm_iam --activate
orchestrate env list

cd tools
orchestrate tools import -k python -r "requirements.txt" -f "tools.py"

cd ../agents
orchestrate agents import -f tfsa_policy_agent.yaml
orchestrate agents import -f tfsa_calculation_agent.yaml
orchestrate agents import -f tfsa_transaction_agent.yaml
orchestrate agents import -f tfsa_orchestrator_agent.yaml
cd ..
