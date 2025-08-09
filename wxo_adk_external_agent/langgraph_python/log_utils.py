# log_utils.py
import glob
import json
import os
from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

# Create logs directory if it doesn't exist
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)


def log_access(user_id: str, thread_id: str, is_stream: bool, user_input: str, response: str, model: str):
    """Log user access with input and response"""
    log_entry = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stream": is_stream,
        "user_id": user_id,
        "thread_id": thread_id,
        "model": model,
        "user_input": user_input,
        "response": response
    }

    # Create daily log file
    today = datetime.today().strftime("%Y-%m-%d")
    log_file = os.path.join(LOG_DIR, f"access_{today}.log")

    with open(log_file, "a") as f:
        f.write(json.dumps(log_entry) + "\n")


# Create router for log endpoints
log_router = APIRouter()


# View access logs
@log_router.get("/logs", response_class=HTMLResponse)
async def view_access_logs():
    """Endpoint to view access logs"""
    # Get all log files
    log_files = glob.glob(os.path.join(LOG_DIR, "access_*.log"))
    log_entries = []

    # Read all log entries
    for log_file in log_files:
        try:
            with open(log_file, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        log_entries.append(entry)
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue

    # Sort by timestamp descending
    log_entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    # Generate HTML table
    html_content = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Access Logs</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 20px; }
            table { border-collapse: collapse; width: 100%; }
            th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }
            th { background-color: #f2f2f2; }
            tr:nth-child(even) { background-color: #f9f9f9; }
            .log-container { 
                overflow-y: auto; 
                margin-bottom: 20px;
            }
            .timestamp { white-space: nowrap; }
        </style>
    </head>
    <body>
        <h1>Access Logs</h1>
        <div class="log-container">
            <table>
                <thead>
                    <tr>
                        <th>Timestamp</th>
                        <th>User ID</th>
                        <th>Thread ID</th>
                        <th>Stream</th>
                        <th>Model</th>
                        <th>User Input</th>
                        <th>Response</th>
                    </tr>
                </thead>
                <tbody>
    """

    for entry in log_entries:
        html_content += f"""
        <tr>
            <td class="timestamp">{entry.get('timestamp', '')}</td>
            <td>{entry.get('user_id', '')}</td>
            <td>{entry.get('thread_id', '')}</td>
            <td>{entry.get('stream', '')}</td>
            <td>{entry.get('model', '')}</td>
            <td>{entry.get('user_input', '')}</td>
            <td>{entry.get('response', '')}</td>
        </tr>
        """

    html_content += """
                </tbody>
            </table>
        </div>
    </body>
    </html>
    """

    return HTMLResponse(content=html_content)
