#!/usr/bin/env python3
"""Plan -> images -> narration -> SFX -> music -> 25s MP4 -> YouTube. Costs $0."""
import json, os, random, sqlite3, subprocess, time
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
TARGET = 24.0                                # target length in seconds
OUT = Path("output"); OUT.mkdir(exist_ok=True)
DB  = Path("data/history.db")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
GEMINI     = os.environ["GEMINI_API_KEY"]
FS_KEY     = os.environ.get("FREESOUND_API_KEY", "")
YT_ID      = os.environ["YOUTUBE_CLIENT_ID"]
YT_SEC     = os.environ["YOUTUBE_CLIENT_SECRET"]
YT_REFRESH = os.environ["YOUTUBE_REFRESH_TOKEN"]
VOICE      = os.environ.get("KOKORO_VOICE", "af_heart")

def sh(*cmd):
    subprocess.run(cmd, check=True, capture_output=True)

def dur(p):
    return float(subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
        "-of","csv=p=0",str(p)], capture_output=True, text=True).stdout.strip())

def db():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS videos(ts,title,idea,vid,status)")
    return c

# ---------- 1. PLAN (Gemini free tier) ----------
PLAN_PROMPT = """You are the creative director of a family-friendly YouTube Shorts channel
of funny ANIMATED animal stories (bright 3D-cartoon style).
