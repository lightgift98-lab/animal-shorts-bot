#!/usr/bin/env python3
"""Verify the YouTube refresh token still works and report channel state.

Run from Actions (where the secrets live). Cheap: 2 quota units.
"""
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.force-ssl"]


def main():
    missing = [k for k in ("YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                           "YOUTUBE_REFRESH_TOKEN") if not os.environ.get(k)]
    if missing:
        print("MISSING SECRETS:", ", ".join(missing))
        return 1

    cred = Credentials(
        None,
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    try:
        cred.refresh(Request())
    except Exception as e:                                    # noqa: BLE001
        print("REFRESH FAILED:", e)
        print("\nIf this says invalid_grant, the token was revoked or expired "
              "-> re-run auth_setup.py locally and update the secret.")
        return 1

    print("token refresh: OK")
    print("access token expires:", cred.expiry, "UTC (short-lived, expected)")

    yt = build("youtube", "v3", credentials=cred)
    ch = yt.channels().list(part="snippet,statistics,status", mine=True).execute()
    if not ch.get("items"):
        print("no channel visible for this account")
        return 1
    c = ch["items"][0]
    st = c["statistics"]
    print(f"channel: {c['snippet']['title']}  (id {c['id']})")
    print(f"videos: {st.get('videoCount')}  subs: {st.get('subscriberCount')}  "
          f"views: {st.get('viewCount')}")

    # a published (non-Testing) OAuth app issues refresh tokens that do not
    # expire after 7 days; nothing to assert here, but the successful refresh
    # above is the practical proof the credentials are live right now
    return 0


if __name__ == "__main__":
    sys.exit(main())
