import os

import requests
from dotenv import load_dotenv
from flask import Flask, redirect, request, session, jsonify
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

load_dotenv('.env')

app = Flask(__name__)
app.secret_key = os.urandom(24)  # Secret key for session

# Google OAuth Configuration
GOOGLE_OAUTH_CLIENT_ID = os.environ['GOOGLE_OAUTH_WEB_APP_CLIENT_ID']
GOOGLE_OAUTH_CLIENT_SECRET = os.environ['GOOGLE_OAUTH_WEB_APP_CLIENT_SECRET']
GOOGLE_OAUTH_REDIRECT_URI = os.environ['GOOGLE_OAUTH_WEB_APP_REDIRECT_URI']


# Initiates Google OAuth flow
@app.route('/login')
def login():
    # Generate Google OAuth URL
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_OAUTH_CLIENT_ID}&"
        f"response_type=code&"
        f"scope=openid%20email%20profile&"
        f"redirect_uri={GOOGLE_OAUTH_REDIRECT_URI}&"
        f"access_type=offline"
    )
    return redirect(auth_url)


# Handles auth response & token exchange
@app.route('/oauth2callback')
def oauth2callback():
    # Exchange authorization code for tokens
    code = request.args.get('code')
    print(f"code={code}")
    token_url = "https://oauth2.googleapis.com/token"
    token_data = {
        'code': code,
        'client_id': GOOGLE_OAUTH_CLIENT_ID,
        'client_secret': GOOGLE_OAUTH_CLIENT_SECRET,
        'redirect_uri': GOOGLE_OAUTH_REDIRECT_URI,
        'grant_type': 'authorization_code'
    }
    token_response = requests.post(token_url, data=token_data).json()

    # Validate ID token (critical for security)
    id_info = id_token.verify_oauth2_token(
        token_response['id_token'],
        google_requests.Request(),
        GOOGLE_OAUTH_CLIENT_ID
    )

    # Session management: Maintains authenticated user state
    # Extract user information
    session['user_email'] = id_info['email']
    session['user_name'] = id_info.get('name', '')

    # Here you would integrate with MCP
    # mcp_integration(session['user_email'])

    return redirect('/protected')


@app.route('/protected')
def protected():
    if 'user_email' not in session:
        return redirect('/login')

    return jsonify({
        "email": session['user_email'],
        "name": session['user_name'],
        "message": "Authenticated via Google SSO"
    })


@app.route('/logout')
def logout():
    session.clear()
    return "Logged out"


if __name__ == '__main__':
    app.run(host='0.0.0.0',
            ssl_context='adhoc')  # HTTPS required for Google OAuth
