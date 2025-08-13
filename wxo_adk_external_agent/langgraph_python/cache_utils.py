# cache_utils.py
import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from tfsa_assistant_graph import cache

# Create router for cache endpoints
cache_router = APIRouter()


# End point to manage cache
@cache_router.get("/cache", response_class=HTMLResponse)
async def manage_cache():
    """Endpoint to manage TFSA assistant cache"""
    cache_data = cache.get_all()
    cache_enabled = cache.is_enabled()

    # Generate HTML with cache controls
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>TFSA Assistant Cache</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; }}
            .cache-controls {{ margin-bottom: 20px; }}
            .cache-btn {{ 
                padding: 8px 16px; 
                border-radius: 4px; 
                cursor: pointer;
                font-weight: bold;
                margin-right: 10px;
            }}
            .toggle-btn {{ 
                background-color: {"#4CAF50" if cache_enabled else "#f44336"};
                color: white;
                border: none;
            }}
            .clear-btn {{
                background-color: #2196F3;
                color: white;
                border: none;
            }}
            table {{ border-collapse: collapse; width: 100%; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; }}
            th {{ background-color: #f2f2f2; }}
            tr:nth-child(even) {{ background-color: #f9f9f9; }}
            .delete-btn {{ 
                background-color: #ff4d4d; 
                color: white; 
                border: none; 
                padding: 5px 10px; 
                border-radius: 4px; 
                cursor: pointer;
            }}
            .delete-btn:hover {{ background-color: #ff1a1a; }}
            .edit-btn {{ 
                background-color: #4CAF50; 
                color: white; 
                border: none; 
                padding: 5px 10px; 
                border-radius: 4px; 
                cursor: pointer;
                margin-right: 5px;
            }}
            .save-btn {{ 
                background-color: #008CBA; 
                color: white; 
                border: none; 
                padding: 5px 10px; 
                border-radius: 4px; 
                cursor: pointer;
                margin-right: 5px;
            }}
            .cancel-btn {{ 
                background-color: #f44336; 
                color: white; 
                border: none; 
                padding: 5px 10px; 
                border-radius: 4px; 
                cursor: pointer;
            }}
            .cache-status {{
                padding: 8px 16px;
                background-color: {"#4CAF50" if cache_enabled else "#f44336"};
                color: white;
                border-radius: 4px;
                font-weight: bold;
                display: inline-block;
            }}
            .value-container {{
                max-height: 150px;
                overflow-y: auto;
                white-space: pre-wrap;
                word-break: break-all;
                font-family: monospace;
                font-size: 14px;
                padding: 5px;
                background-color: #f8f8f8;
                border: 1px solid #ddd;
                border-radius: 4px;
            }}
            .value-input {{
                width: 100%;
                min-height: 100px;
                font-family: monospace;
                font-size: 14px;
                padding: 5px;
                box-sizing: border-box;
            }}
            .countdown {{
                font-weight: bold;
                color: #e67e22;
                white-space: nowrap;
            }}
            .expired {{
                color: #e74c3c;
                font-weight: bold;
            }}
            .action-cell {{
                white-space: nowrap;
            }}
            .links-section {{
                margin: 30px 0;
                padding: 20px;
                background-color: #f8f9fa;
                border-radius: 8px;
                border: 1px solid #e9ecef;
                overflow: hidden;
            }}
            .links-section h2 {{
                margin-top: 0;
                color: #333;
            }}
            .links-grid {{
                display: grid;
                grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
                gap: 15px;
                margin-top: 15px;
            }}
            .link-card {{
                display: flex;
                flex-direction: column;
                background-color: white;
                border: 1px solid #dee2e6;
                border-radius: 6px;
                padding: 15px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.05);
                height: 100%;
                box-sizing: border-box;
            }}
            .link-card h3 {{
                margin-top: 0;
                color: #007bff;
            }}
            .link-card-content {{
                flex-grow: 1;
            }}
            .link-card a {{
                display: inline-block;
                margin-top: 10px;
                color: #007bff;
                text-decoration: none;
                font-weight: 500;
                align-self: flex-start;
            }}
            .link-card a:hover {{
                text-decoration: underline;
            }}
            .link-description {{
                font-size: 14px;
                color: #6c757d;
                margin: 5px 0;
            }}
        </style>
        <script>
            async function toggleCache() {{
                const response = await fetch('/api/v1/cache/toggle', {{ method: 'POST' }});
                if (response.ok) {{
                    const result = await response.json();
                    alert('Cache is now ' + (result.status ? 'ENABLED' : 'DISABLED'));
                    location.reload();
                }} else {{
                    alert('Failed to toggle cache');
                }}
            }}

            async function clearCache() {{
                if (confirm('Are you sure you want to clear ALL cache items?')) {{
                    const response = await fetch('/api/v1/cache/clear', {{ method: 'POST' }});
                    if (response.ok) {{
                        alert('Cache cleared successfully!');
                        location.reload();
                    }} else {{
                        alert('Failed to clear cache');
                    }}
                }}
            }}

            async function deleteItem(key) {{
                if (confirm('Are you sure you want to delete this cache item?')) {{
                    const response = await fetch(`/api/v1/cache/${{key}}`, {{ method: 'DELETE' }});
                    if (response.ok) {{
                        alert('Item deleted successfully!');
                        location.reload();
                    }} else {{
                        alert('Failed to delete item');
                    }}
                }}
            }}

            function editItem(key) {{
                const valueCell = document.getElementById(`value-${{key}}`);
                const actionCell = document.getElementById(`actions-${{key}}`);
                const currentValue = valueCell.textContent;

                // Replace value display with textarea
                valueCell.innerHTML = `
                    <textarea class="value-input" id="edit-input-${{key}}">${{currentValue}}</textarea>
                `;

                // Replace action buttons
                actionCell.innerHTML = `
                    <button class="save-btn" onclick="saveItem('${{key}}')">Save</button>
                    <button class="cancel-btn" onclick="cancelEdit('${{key}}')">Cancel</button>
                `;
            }}

            async function saveItem(key) {{
                const newValue = document.getElementById(`edit-input-${{key}}`).value;

                const response = await fetch(`/api/v1/cache/${{key}}`, {{
                    method: 'PUT',
                    headers: {{
                        'Content-Type': 'application/json'
                    }},
                    body: JSON.stringify({{ value: newValue }})
                }});

                if (response.ok) {{
                    alert('Item updated successfully!');
                    location.reload();
                }} else {{
                    alert('Failed to update item');
                }}
            }}

            function cancelEdit(key) {{
                location.reload();
            }}

            // Function to update countdown timers
            function updateCountdowns() {{
                const now = Math.floor(Date.now() / 1000);
                document.querySelectorAll('.countdown').forEach(element => {{
                    const expiresAt = parseInt(element.dataset.expiresAt);
                    if (expiresAt <= 0) {{
                        element.textContent = "Never expires";
                    }} else {{
                        const secondsLeft = expiresAt - now;
                        if (secondsLeft <= 0) {{
                            element.textContent = "EXPIRED";
                            element.classList.add('expired');

                            // Remove row after 2 seconds
                            setTimeout(() => {{
                                const row = element.closest('tr');
                                if (row) row.remove();
                            }}, 2000);
                        }} else {{
                            const hours = Math.floor(secondsLeft / 3600);
                            const minutes = Math.floor((secondsLeft % 3600) / 60);
                            const seconds = secondsLeft % 60;
                            element.textContent = `${{hours}}h ${{minutes}}m ${{seconds}}s`;
                        }}
                    }}
                }});
            }}

            // Initialize countdown timers
            document.addEventListener('DOMContentLoaded', () => {{
                updateCountdowns();
                setInterval(updateCountdowns, 1000);
            }});
        </script>
    </head>
    <body>
        <div class="header">
            <h1>TFSA Assistant Cache</h1>
            <div class="cache-status">
                Cache Status: {cache_enabled}
            </div>
        </div>

        <div class="cache-controls">
            <button class="cache-btn toggle-btn" onclick="toggleCache()">
                {"Disable" if cache_enabled else "Enable"} Cache
            </button>
            <button class="cache-btn clear-btn" onclick="clearCache()">
                Clear Entire Cache
            </button>
        </div>

        <p>Total items: {len(cache_data)}</p>
        <table>
            <thead>
                <tr>
                    <th>Key</th>
                    <th>Value</th>
                    <th>Time Remaining</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
    """

    # Add table rows with full values in scrollable containers
    for key, item in cache_data.items():
        # Skip cached state
        if key.startswith("thread_state_"):
            continue
        metadata = item.get("metadata", "")
        expires_at = item["expires_at"]
        value_str = str(item["value"])

        # Format expiration time
        expires_display = "Never"
        if expires_at > 0:
            expires_dt = datetime.fromtimestamp(expires_at)
            expires_display = expires_dt.strftime('%Y-%m-%d %H:%M:%S')

        # Add table row
        html_content += f"""
                <tr>
                    <td>{key}<br/>Expire:&nbsp;{expires_display}<br/>Metadata:&nbsp;{metadata}</td>
                    <td id="value-{key}" class="value-container">{value_str}</td>
                    <td class="countdown" data-expires-at="{expires_at}">
                        Calculating...
                    </td>
                    <td id="actions-{key}" class="action-cell">
                        <button class="edit-btn" onclick="editItem('{key}')">Edit</button>
                        <button class="delete-btn" onclick="deleteItem('{key}')">Delete</button>
                    </td>
                </tr>
                """
    # Close table
    html_content += """
                </tbody>
            </table>
            
            <div class="links-section">
                <h2>TFSA Assistant Resources</h2>
                <div class="links-grid">
                    <div class="link-card">
                        <div class="link-card-content">
                            <h3>API Documentation</h3>
                            <p class="link-description">Interactive API documentation for the TFSA Assistant</p>
                        </div>
                        <a href="/api/v1/docs" target="_blank">Open API Docs</a>
                    </div>
                    <div class="link-card">
                        <div class="link-card-content">
                            <h3>Access Logs</h3>
                            <p class="link-description">View access logs for the TFSA Assistant</p>
                        </div>
                        <a href="/api/v1/logs" target="_blank">View Access Logs</a>
                    </div>
                    <div class="link-card">
                        <div class="link-card-content">
                            <h3>MLflow Tracking</h3>
                            <p class="link-description">Monitor agent workflow performance and experiments</p>
                        </div>
                        <a href="/" target="_blank">Open MLflow</a>
                    </div>
                    <div class="link-card">
                        <div class="link-card-content">
                            <h3>Technical Blog</h3>
                            <p class="link-description">Beyond Basics: Developing Advanced External Agents with IBM watsonx Orchestrate</p>
                        </div>
                        <a href="https://medium.com/@jerry.shao/beyond-basics-developing-advanced-external-agents-with-ibm-watsonx-orchestrate-18db983796b7" target="_blank">Read on Medium</a>
                    </div>
                    <div class="link-card">
                        <div class="link-card-content">
                            <h3>Source Code</h3>
                            <p class="link-description">GitHub repository for the TFSA LangGraph Assistant</p>
                        </div>
                        <a href="https://github.com/jerryshao2012/agentic-ai-tfsa/tree/main/wxo_adk_external_agent/langgraph_python" target="_blank">View on GitHub</a>
                    </div>
                </div>
            </div>
        
        </body>
        </html>
        """

    return HTMLResponse(content=html_content)


# Add new endpoints for cache control
@cache_router.post("/cache/toggle")
async def toggle_cache():
    """Toggle cache enabled state"""
    current_state = not cache.is_enabled()
    cache.set_enabled(current_state)
    return {"status": current_state, "message": f"Cache is now {'ENABLED' if current_state else 'DISABLED'}"}


@cache_router.post("/cache/clear")
async def clear_cache():
    """Clear entire cache"""
    cache_data = cache.get_all()

    # Delete all items
    for key in list(cache_data.keys()):
        cache.delete(key)

    return {"status": "success", "message": f"Cleared {len(cache_data)} cache items"}


@cache_router.delete("/cache/{cache_key}")
async def delete_cache_item(cache_key: str):
    """Delete a specific cache item"""
    if cache.delete(cache_key):
        return {"status": "success", "message": f"Cache item {cache_key} deleted"}
    raise HTTPException(status_code=404, detail="Item not found")


@cache_router.put("/cache/{cache_key}")
async def update_cache_item(cache_key: str, item: dict):
    """Update a specific cache item"""
    try:
        # Try to parse the value as JSON, if it fails, store as string
        try:
            new_value = json.loads(item.get("value"))
        except (json.JSONDecodeError, TypeError):
            new_value = item.get("value")

        # Get the existing item to preserve metadata and expiration
        existing_item = cache.load_from_cache(cache_key)
        if not existing_item:
            raise HTTPException(status_code=404, detail="Item not found")

        # Preserve existing metadata and expiration
        metadata = existing_item.get("metadata", {})
        expires_at = existing_item.get("expires_at", 0)

        # Update the cache with new value
        cache.cache(cache_key, new_value, metadata=metadata, expires_at=expires_at)

        return {"status": "success", "message": f"Cache item {cache_key} updated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update item: {str(e)}")
