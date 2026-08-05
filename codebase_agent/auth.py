# ==========================================
# auth.py — OAuth device/browser flow.
# UNCHANGED from the original script (out of scope for this pass):
# perform_login, refresh_access_token, fetch_available_model, and the
# local callback server they depend on.
# ==========================================
import base64
import hashlib
import os
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests

from config import (
    AUTH_URL_BASE,
    CALLBACK_PATH,
    CLIENT_ID,
    PORT,
    REDIRECT_URI,
    TOKEN_URL,
)

# GLOBAL STATE
auth_code = None
expected_state = None


class CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global auth_code, expected_state
        parsed_url = urllib.parse.urlparse(self.path)

        if parsed_url.path == CALLBACK_PATH:
            query_components = urllib.parse.parse_qs(parsed_url.query)
            if 'code' in query_components:
                returned_state = query_components.get('state', [''])[0]
                if returned_state != expected_state:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b"Authentication failed: State mismatch.")
                    return
                auth_code = query_components['code'][0]
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                success_html = "<html><body><h2>Authentication Successful!</h2><p>You can safely close this window.</p></body></html>"
                self.wfile.write(success_html.encode('utf-8'))
            else:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()


def perform_login():
    global auth_code, expected_state
    auth_code = None
    print("[*] Starting new browser login flow...")

    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).decode('utf-8').rstrip('=')
    sha256_hash = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(sha256_hash).decode('utf-8').rstrip('=')
    expected_state = os.urandom(16).hex()

    auth_params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": "openid profile email offline_access",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": expected_state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "custom_python_script"
    }

    auth_url = f"{AUTH_URL_BASE}?{urllib.parse.urlencode(auth_params)}"
    server = HTTPServer(('localhost', PORT), CallbackHandler)
    webbrowser.open(auth_url)

    while not auth_code:
        server.handle_request()

    print("[+] Exchanging code for tokens...")
    token_payload = {
        "grant_type": "authorization_code",
        "client_id": CLIENT_ID,
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier
    }
    response = requests.post(TOKEN_URL, data=token_payload)
    if response.status_code == 200:
        return response.json()
    return None


def refresh_access_token(refresh_token):
    payload = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID}
    response = requests.post(TOKEN_URL, data=payload)
    return response.json() if response.status_code == 200 else None


def fetch_available_model(access_token):
    url = "https://chatgpt.com/backend-api/codex/models?client_version=0.146.0"
    headers = {"Authorization": f"Bearer {access_token}"}
    response = requests.get(url, headers=headers)
    if response.status_code == 401:
        return None, False
    print("[*] Enforcing model: gpt-5.6-terra")
    return "gpt-5.6-terra", True