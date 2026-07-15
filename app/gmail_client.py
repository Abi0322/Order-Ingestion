"""
gmail_client.py — Optional Gmail integration.

Isolated from the rest of the app so the app runs fine without Gmail
credentials configured (manual paste-in still works). To enable Gmail
fetching:
  1. Create OAuth credentials in Google Cloud Console (Desktop app type)
  2. Download as credentials.json, place it in this app/ directory
  3. On first run, a browser window will prompt you to authorize access
  4. A token.json will be created locally to cache the authorization

NEITHER credentials.json NOR token.json should ever be committed or shared —
they grant access to a real Gmail account. Both are gitignored.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
TOKEN_PATH = Path(__file__).parent / "token.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def gmail_available() -> bool:
    """Check whether Gmail integration can be used at all."""
    try:
        import googleapiclient.discovery  # noqa: F401
        import google_auth_oauthlib.flow  # noqa: F401
    except ImportError:
        return False
    return CREDENTIALS_PATH.exists()


def get_gmail_service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())

    return build("gmail", "v1", credentials=creds)


def fetch_latest_emails(n: int = 5, from_email: Optional[str] = None) -> List[Dict[str, Any]]:
    service = get_gmail_service()
    query = f"from:{from_email}" if from_email else ""
    results = service.users().messages().list(userId="me", q=query, maxResults=n).execute()
    messages = results.get("messages", [])

    emails = []
    for m in messages:
        msg = service.users().messages().get(userId="me", id=m["id"]).execute()
        payload = msg["payload"]

        body = ""
        if "parts" in payload:
            for part in payload["parts"]:
                if part["mimeType"] == "text/plain":
                    data = part["body"].get("data")
                    if data:
                        body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
                        break
        else:
            data = payload["body"].get("data")
            if data:
                body = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")

        headers = {h["name"]: h["value"] for h in payload["headers"]}
        emails.append(
            {
                "sender": extract_company(headers.get("From", "")),
                "subject": headers.get("Subject", ""),
                "body": body,
                "message_id": m["id"],  # Gmail's own unique ID — lets the
                # caller skip messages already imported as an order, since
                # "fetch latest N" re-lists the same recent messages on
                # every call.
            }
        )
    return emails


def extract_company(sender: str) -> str:
    match = re.match(r'"?([^"<]+)"?\s*<', sender)
    return match.group(1).strip() if match else sender
