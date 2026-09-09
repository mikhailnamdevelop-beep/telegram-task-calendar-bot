"""One-time OAuth bootstrap for Google Calendar.

The script prints a refresh token to the current console only. It does not create
or modify .env files. Copy the result into the deployment secret store.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Obtain a Google Calendar refresh token"
    )
    parser.add_argument(
        "--client-secrets",
        type=Path,
        help="Path to an untracked Google OAuth desktop-client JSON file",
    )
    parser.add_argument(
        "--port", type=int, default=0, help="Local callback port (0 = automatic)"
    )
    return parser


def _flow(args: argparse.Namespace) -> InstalledAppFlow:
    if args.client_secrets:
        if not args.client_secrets.is_file():
            raise SystemExit(f"Client secrets file not found: {args.client_secrets}")
        return InstalledAppFlow.from_client_secrets_file(
            str(args.client_secrets), SCOPES
        )
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise SystemExit(
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET, or pass --client-secrets."
        )
    return InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        SCOPES,
    )


def main() -> int:
    args = _parser().parse_args()
    flow = _flow(args)
    credentials = flow.run_local_server(
        host="localhost",
        port=args.port,
        authorization_prompt_message="Open this URL in your browser:\n{url}",
        success_message="Authorization complete. You can close this browser tab.",
        access_type="offline",
        prompt="consent",
    )
    if not credentials.refresh_token:
        print(
            "No refresh token was returned. Revoke the app grant and retry with consent.",
            file=sys.stderr,
        )
        return 1
    print("\nGOOGLE_REFRESH_TOKEN (copy now; this script does not save it):")
    print(credentials.refresh_token)
    print("\nStore it in your deployment secret manager. Do not commit it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
