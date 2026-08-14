#!/usr/bin/env python3
"""Offline test of the ffmpeg assembly path (no API keys, no network).

Fakes: plan JSON, the 5 Pollinations images, and the Kokoro narration wav.
Exercises the real bake / make_clips / mix / thumbnail code from pipeline.py.
"""
import os
import wave
import math
import struct
import sys
from pathlib import Path

os.environ.setdefault("GEMINI_API_KEY", "test")
os.environ.setdefault("DRY_RUN", "true")

import pipeline as P  # noqa: E402
from PIL import Image  # noqa: E402

OUT = P.OUT
OUT.mkdir(exist_ok=True)

PLAN = {
    "idea": "cat vs cucumber",
    "character": "a fluffy orange cartoon cat",
    "title": "This Cat Met A Cucumber 😂 #shorts",
    "description": "He did not take it well. Wait for the ending!",
    "tags": ["funny animals", "cat", "shorts", "cartoon", "cute"],
    "thumbnail_text": "WAIT FOR IT",
    "scenes": [
        {"image_prompt": "cat naps", "narration": "Meet Milo, a cat who fears nothing at all.",
         "caption": "Milo fears nothing", "sfx_query": "cartoon boing"},
        {"image_prompt": "cucumber", "narration": "Until his human placed a cucumber behind him.",
         "caption": "Enter the cucumber", "sfx_query": "whoosh"},
        {"image_prompt": "turn", "narration": "Milo turned around very slowly indeed.",
         "caption": "Slow turn", "sfx_query": "suspense"},
        {"image_prompt": "jump", "narration": "And launched himself straight into orbit!",
         "caption": "LIFTOFF", "sfx_query": "spring"},
        {"image_prompt": "hide", "narration": "Milo now supervises all vegetables from the shelf.",
         "caption": "Forever watching", "sfx_query": "cartoon pop"},
    ],
}

COLORS = [(240, 140, 60), (90, 180, 220), (150, 220, 130), (230, 110, 160), (250, 210, 90)]
for i, c in enumerate(COLORS):
    img = Image.new("RGB", (P.W, P.H), c)
    for y in range(0, P.H, 4):          # cheap gradient so zoompan has detail to move
        for x in range(0, P.W, 160):
            img.putpixel((x, y), (255, 255, 255))
    img.save(OUT / f"img{i}.jpg", quality=90)
print("fake images written")

# fake narration: ~19s of quiet tone
sr, secs = 24000, 19.0
with wave.open(str(OUT / "narration.wav"), "w") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes(b"".join(
        struct.pack("<h", int(2500 * math.sin(2 * math.pi * 180 * (n / sr))))
        for n in range(int(sr * secs))))
print(f"fake narration: {P.dur(OUT/'narration.wav'):.2f}s")

nd = P.dur(OUT / "narration.wav")
atempo = max(1.0, min(nd / (P.TARGET - 1.2), 1.18))
video_dur = min(max(P.TARGET, nd / atempo + 1.2), 29.0)
weights = [len(s["narration"].split()) or 1 for s in PLAN["scenes"]]
tot = sum(weights)
starts, t = [], 0.0
for w_ in weights:
    starts.append(t); t += w_ / tot * video_dur
starts.append(video_dur)
print(f"atempo={atempo:.3f} video_dur={video_dur:.2f} starts={[round(s,2) for s in starts]}")

P.make_clips(PLAN, starts, video_dur)
print(f"noaudio.mp4 = {P.dur(OUT/'noaudio.mp4'):.2f}s")

credits = P.mix(PLAN, starts, video_dur, atempo)
print(f"final.mp4 = {P.dur(OUT/'final.mp4'):.2f}s  credits={credits}")

P.thumbnail(PLAN, starts)
tb = (OUT / "thumb.jpg").stat().st_size
print(f"thumb.jpg = {tb//1024} KB")

assert 24.0 <= P.dur(OUT / "final.mp4") <= 29.5, "duration out of Shorts range"
assert tb < 2_000_000, "thumbnail over YouTube 2MB cap"
print("\nASSEMBLY TEST PASSED")
