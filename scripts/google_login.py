"""Connect the agent to your Gmail (read only) and Google Calendar (read + add events). Run once on your Mac:

  1. console.cloud.google.com > your project > APIs & Services > Library: enable "Gmail API" and "Google Calendar API".
  2. APIs & Services > OAuth consent screen: External, add your Gmail as a test user.
  3. APIs & Services > Credentials > Create credentials > OAuth client ID > Desktop app > Download JSON.
  4. python scripts/google_login.py ~/Downloads/client_secret_XXXX.json
     A browser opens; sign in and allow. This writes data/google_token.json.
  5. bash scripts/server.sh google      (copies the token to the server and restarts the agent)

The token gives read access to your mail: keep data/google_token.json private. Revoke any time at
myaccount.google.com/permissions.
"""
import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.gsuite import SCOPES, TOKEN_FILE  # noqa: E402

if len(sys.argv) < 2:
    sys.exit("Usage: python scripts/google_login.py path/to/client_secret.json")
flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
with open(TOKEN_FILE, "w") as f:
    json.dump({"client_id": creds.client_id, "client_secret": creds.client_secret, "refresh_token": creds.refresh_token}, f)
os.chmod(TOKEN_FILE, 0o600)
print(f"Connected. Saved {TOKEN_FILE}. Now run: bash scripts/server.sh google")
