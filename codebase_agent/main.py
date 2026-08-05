# ==========================================
# main.py — entrypoint: load/refresh the saved token, then kick off the
# chat loop. UNCHANGED logic from the original script's `main()`.
# ==========================================
import json
import os

from auth import fetch_available_model, perform_login, refresh_access_token
from agent import start_interactive_chat
from config import TOKEN_FILE


def main():
    tokens = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'r') as f:
            tokens = json.load(f)

    if tokens and 'access_token' in tokens:
        chosen_model, is_valid = fetch_available_model(tokens['access_token'])
        if not is_valid and 'refresh_token' in tokens:
            new_tokens = refresh_access_token(tokens['refresh_token'])
            if new_tokens:
                if 'refresh_token' not in new_tokens:
                    new_tokens['refresh_token'] = tokens['refresh_token']
                with open(TOKEN_FILE, 'w') as f:
                    json.dump(new_tokens, f)
                chosen_model, _ = fetch_available_model(new_tokens['access_token'])
                if chosen_model:
                    start_interactive_chat(new_tokens['access_token'], chosen_model)
                return
            else:
                tokens = None
        elif is_valid and chosen_model:
            start_interactive_chat(tokens['access_token'], chosen_model)
            return

    if not tokens:
        tokens = perform_login()
        if tokens:
            with open(TOKEN_FILE, 'w') as f:
                json.dump(tokens, f)
            chosen_model, _ = fetch_available_model(tokens['access_token'])
            if chosen_model:
                start_interactive_chat(tokens['access_token'], chosen_model)


if __name__ == "__main__":
    main()
