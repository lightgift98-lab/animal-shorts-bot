#!/usr/bin/env python3
"""Delete videos this bot uploaded, then clear them from the history DB.

Usage (inside GitHub Actions, where the YouTube secrets exist):
    python cleanup.py            # delete every video logged as 'published'
    python cleanup.py ID1 ID2    # delete only the given video IDs
"""
import os
import sqlite3
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

DB = Path("data/history.db")
YT_ID = os.environ["YOUTUBE_CLIENT_ID"]
YT_SEC = os.environ["YOUTUBE_CLIENT_SECRET"]
YT_REFRESH = os.environ["YOUTUBE_REFRESH_TOKEN"]


def service():
    cred = Credentials(
        None, client_id=YT_ID, client_secret=YT_SEC, refresh_token=YT_REFRESH,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube",
                "https://www.googleapis.com/auth/youtube.force-ssl"],
    )
    cred.refresh(Request())
    return build("youtube", "v3", credentials=cred)


def logged_ids():
    if not DB.exists():
        return []
    con = sqlite3.connect(DB)
    return [r[0] for r in con.execute(
        "SELECT vid FROM videos WHERE status='published' AND vid IS NOT NULL AND vid != ''")]


def main():
    ids = sys.argv[1:] or logged_ids()
    if not ids:
        print("nothing to delete")
        return 0

    yt = service()
    print(f"deleting {len(ids)} video(s): {', '.join(ids)}")
    gone = []
    for vid in ids:
        try:
            yt.videos().delete(id=vid).execute()
            print(f"  deleted {vid}")
            gone.append(vid)
        except HttpError as e:
            # 404 means it is already gone - treat as success so the DB gets cleaned
            if e.resp.status == 404:
                print(f"  {vid} not found (already deleted)")
                gone.append(vid)
            else:
                print(f"  FAILED {vid}: {e}")

    if gone and DB.exists():
        con = sqlite3.connect(DB)
        con.executemany("UPDATE videos SET status='deleted' WHERE vid=?",
                        [(v,) for v in gone])
        con.commit()
        print(f"marked {len(gone)} row(s) as deleted in history")

    # report what is left on the channel
    try:
        ch = yt.channels().list(part="statistics", mine=True).execute()
        print("channel videoCount now:",
              ch["items"][0]["statistics"].get("videoCount"))
    except Exception as e:
        print("could not read channel stats:", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
