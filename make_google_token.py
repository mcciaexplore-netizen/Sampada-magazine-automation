"""One-off Google sign-in: creates google_token.json for GOOGLE_OAUTH_TOKEN_JSON on Render.

Run:  .\\.venv\\Scripts\\python.exe make_google_token.py
Needs google_credentials.json (an OAuth *Desktop app* client) in this folder.
"""
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

from google_drive import SCOPES

ROOT = Path(__file__).parent
flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / "google_credentials.json"), SCOPES)
creds = flow.run_local_server(port=8765, open_browser=False, authorization_prompt_message=(
    "\nOpen this link in your browser (copy the WHOLE line, it is long):\n\n{url}\n"))
(ROOT / "google_token.json").write_text(creds.to_json(), encoding="utf-8")
print("\nSaved google_token.json - paste its full contents into GOOGLE_OAUTH_TOKEN_JSON on Render.")
