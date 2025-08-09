# cache_utils.py
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from tfsa_assistant import cache

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
            .countdown {{
                font-weight: bold;
                color: #e67e22;
            }}
            .expired {{
                color: #e74c3c;
                font-weight: bold;
            }}
        </style>
        <script>
            async function toggleCache() {{
                const response = await fetch('/cache/toggle', {{ method: 'POST' }});
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
                    const response = await fetch('/cache/clear', {{ method: 'POST' }});
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
                    const response = await fetch(`/cache/${{key}}`, {{ method: 'DELETE' }});
                    if (response.ok) {{
                        alert('Item deleted successfully!');
                        location.reload();
                    }} else {{
                        alert('Failed to delete item');
                    }}
                }}
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
                    <th>Action</th>
                </tr>
            </thead>
            <tbody>
    """

    # Add table rows with full values in scrollable containers
    for key, item in cache_data.items():
        metadata = item.get("metadata", "")
        expires_at = item["expires_at"]
        value_str = str(item["value"])

        # Format expiration time
        expires_display = "Never"
        if expires_at > 0:
            expires_dt = datetime.fromtimestamp(expires_at)
            expires_display = expires_dt.strftime('%Y-%m-%d %H:%M:%S')

        html_content += f"""
        <tr>
            <td>{key}<br/>{expires_display}<br/>{metadata}</td>
            <td class="value-container">{value_str}</td>
            <td class="countdown" data-expires-at="{expires_at}">
                Calculating...
            </td>
            <td>
                <button class="delete-btn" onclick="deleteItem('{key}')">
                    Delete
                </button>
            </td>
        </tr>
        """

    html_content += """
            </tbody>
        </table>
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
