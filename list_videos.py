#!/usr/bin/env python3
"""List everything actually on the channel (uploads playlist is authoritative;
channel.statistics.videoCount is cached and lags behind deletions)."""
import os, sys
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

cred = Credentials(None,
    client_id=os.environ["YOUTUBE_CLIENT_ID"],
    client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
    refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.force-ssl"])
cred.refresh(Request())
yt = build("youtube","v3",credentials=cred)

ch = yt.channels().list(part="contentDetails,statistics", mine=True).execute()["items"][0]
up = ch["contentDetails"]["relatedPlaylists"]["uploads"]
print("cached videoCount:", ch["statistics"].get("videoCount"))
print("uploads playlist:", up)

items, tok = [], None
while True:
    r = yt.playlistItems().list(part="snippet,status", playlistId=up,
                                maxResults=50, pageToken=tok).execute()
    items += r.get("items", [])
    tok = r.get("nextPageToken")
    if not tok: break

print(f"\nACTUAL videos on channel: {len(items)}")
for it in items:
    s = it["snippet"]
    print(f"  {s['resourceId']['videoId']}  {s['title'][:60]}  "
          f"[{it.get('status',{}).get('privacyStatus')}]  {s['publishedAt']}")
