1. Using the MCP Python SDK installation.
mcp version: Check the version
mcp run: Run the MCP server
mcp dev: Run the MCP server with MCP Inspector
mcp install: Connect the MCP server to Claude Desktop
```shell
pip install "mcp[cli]"
mcp run tfsa_mcp_server.py
mcp dev tfsa_mcp_server.py
# This command not install server well. Need to manually update configuration file claude_desktop_config.json
mcp install /Users/jerryshao/Documents/docs/Software/AI/structured-language-extraction/agentic_workflow/tsfa/tfsa_mcp_server.py --name "TFSA Assistant" -f /Users/jerryshao/Documents/docs/Software/AI/structured-language-extraction/.env
```
claude_desktop_config.json
```json
{
  "mcpServers": {
    "TFSA Assistant": {
      "command": "uv",
      "args": [
          "--directory",
          "/PATH_TO_PROJECT/tsfa",
          "run",
          "tfsa_mcp_server.py"
      ],
      "env": {
        "LOGGING_LEVEL": "DEBUG",
        "IBM_CLOUD_URL": "https://us-south.ml.cloud.ibm.com",
        "PROJECT_ID": "2e0bdaa6-5841-4992-af94-50132e6b10cc",
        "API_KEY": "1UGPkvU7CsoqVn3QwFSM43bQS-a1tYOrp2RafW9uxxxx",
        "OLLAMA_ENDPOINT_URL": "http://localhost:11434",
        "DEEPSEEK_API_KEY": "sk-8de4a6f3df3448d583009b6a97aexxxx",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "TAVILY_API_KEY": "tvly-h0npH0s2aSByVHeShBdQsXNML0vAxxxx"
      }
    },
    "e-transfer Assistant": {
      "command": "uv",
      "args": [
          "--directory",
          "/PATH_TO_PROJECT/tsfa",
          "run",
          "e_transfer_mcp_server.py"
      ],
      "env": {
        "LOGGING_LEVEL": "DEBUG",
        "IBM_CLOUD_URL": "https://us-south.ml.cloud.ibm.com",
        "PROJECT_ID": "2e0bdaa6-5841-4992-af94-50132e6b10cc",
        "API_KEY": "1UGPkvU7CsoqVn3QwFSM43bQS-a1tYOrp2RafW9uxxxx",
        "OLLAMA_ENDPOINT_URL": "http://localhost:11434",
        "DEEPSEEK_API_KEY": "sk-8de4a6f3df3448d583009b6a97aexxxx",
        "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
        "TAVILY_API_KEY": "tvly-h0npH0s2aSByVHeShBdQsXNML0vAxxxx"
      }
    } 
  }
}
```
2. Using FastMCP installation.
```shell
pip install fastmcp
python tfsa_mcp_server.py
```

3. Testing Flow for Google OAuth SSO
    1. Access https://localhost:5000/login or https://localhost:5000/protected
    2. Authenticate with Google
    3. Redirected to /protected with user info
    4. Check MCP integration logic

