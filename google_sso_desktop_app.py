import base64
import hashlib
import http.server
import json
import os
import secrets
import socketserver
import threading
import webbrowser
from urllib.parse import urlparse, parse_qs

import requests
from dotenv import load_dotenv
from flask import Flask, request

load_dotenv('.env')

# Google OAuth Configuration
CLIENT_ID = os.environ['GOOGLE_OAUTH_DESKTOP_APP_CLIENT_ID']
# Securely transfers tokens back to desktop app
REDIRECT_URI = os.environ['GOOGLE_OAUTH_DESKTOP_APP_REDIRECT_URI']
DEEP_LINK_SCHEME = os.environ['GOOGLE_OAUTH_DESKTOP_APP_DEEP_LINK_SCHEME']  # Custom deep link scheme


# Prevents authorization code interception attacks
class PKCE:
    @staticmethod
    def generate_verifier():
        return secrets.token_urlsafe(32)

    @staticmethod
    def generate_challenge(verifier):
        challenge = hashlib.sha256(verifier.encode()).digest()
        return base64.urlsafe_b64encode(challenge).decode().replace('=', '')


# Captures OAuth callback on localhost
class AuthServer:
    def __init__(self):
        self.auth_code = None
        self.error = None
        self.server = None

    def start(self):
        handler = http.server.SimpleHTTPRequestHandler
        self.server = socketserver.TCPServer(("0.0.0.0", 8080), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


def initiate_google_login():
    # Generate PKCE codes
    verifier = PKCE.generate_verifier()
    challenge = PKCE.generate_challenge(verifier)

    # Store verifier securely (e.g., in app state)
    app_state = {"pkce_verifier": verifier}

    # Build authorization URL
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={CLIENT_ID}&"
        f"response_type=code&"
        f"scope=openid%20email%20profile&"
        f"redirect_uri={REDIRECT_URI}&"
        f"code_challenge={challenge}&"
        f"code_challenge_method=S256&"
        f"state={json.dumps(app_state)}"
    )

    # Open browser for authentication
    webbrowser.open(auth_url)
    # pass


def exchange_code_for_token(auth_code, verifier):
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        "code": auth_code,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code"
    }

    response = requests.post(token_url, data=payload)
    return response.json()


# Verifies ID token signature with Google's public keys
def verify_google_token(id_token):
    # Use Google's token validation endpoint
    response = requests.get(
        f"https://oauth2.googleapis.com/tokeninfo?id_token={id_token}"
    )
    return response.json() if response.status_code == 200 else None


# Deep Link Server (for redirect back to desktop app)
app = Flask(__name__)


@app.route('/callback')
def oauth_callback():
    auth_code = request.args.get('code')
    # Maintains context between requests (stores PKCE verifier)
    state = json.loads(request.args.get('state', '{}'))

    # Exchange code for tokens
    token_response = exchange_code_for_token(auth_code, state.get('pkce_verifier', ''))

    # Construct deep link URL
    deep_link = f"{DEEP_LINK_SCHEME}?id_token={token_response.get('id_token', '')}"

    return f"""
    <html><body>
        <script>
            window.location.href = "{deep_link}";
        </script>
        <h1>Authentication successful! Returning to application...</h1>
    </body></html>
    """


def start_deep_link_server():
    threading.Thread(target=app.run,
                     kwargs={'host': '0.0.0.0', 'port': 8080}).start()


# Pseudocode for desktop app deep link handling
def handle_deep_link(url):
    parsed = urlparse(url)
    if parsed.scheme == "claude" and parsed.hostname == "auth":
        params = parse_qs(parsed.query)
        id_token = params.get('id_token', [''])[0]

        # Verify and decode ID token
        user_info = verify_google_token(id_token)
        if user_info:
            print(f"Authenticated user: {user_info['email']}")
            # Start authenticated session
        else:
            print("Authentication failed")


# Main application
if __name__ == "__main__":
    # Start local server for OAuth callback
    start_deep_link_server()

    # Initiate Google login
    initiate_google_login()

    # Desktop app should handle deep link:
    # claude://auth?id_token=XYZ
