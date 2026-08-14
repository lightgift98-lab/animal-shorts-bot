# What was broken and what I changed

Your repo at commit `2c4d00f` could not run at all. Below is the audit.

## 1. The blocker: your last commit deleted the file

`2c4d00f "Refactor sh function for readability"` removed **329 of 331 lines**
(14,490 bytes → 1,399). `pipeline.py` ended mid-string inside `PLAN_PROMPT`:

```
SyntaxError: unterminated triple-quoted string literal (detected at line 35)
```

Python could not even parse it, so every scheduled run since died in seconds.
The intact version was still in git at `882b9aa` — I restored it and re-applied
the `sh()` readability change you were going for (now it also surfaces ffmpeg's
stderr instead of hiding it, which is what made the next bug so hard to see).

**Lesson:** that commit was almost certainly a bad copy-paste into the GitHub web
editor. Run `python -m py_compile pipeline.py` before committing.

## 2. The bug that would have broken it anyway: ffmpeg filtergraph indices

In `mix()`:

```python
inputs, filters, labels, n, credits = [], [], [], 0, []   # n starts at 0
...
filters.append(f"[{n}:a]atempo={atempo:.3f},...[nar]")     # -> [0:a]
```

But input `0` is `noaudio.mp4` — the **silent video**. So narration was mapped to
a nonexistent audio stream:

```
Stream specifier ':a' in filtergraph description ... matches no streams.
Error binding filtergraph inputs/outputs: Invalid argument
```

Audio inputs are numbered from **1**. Fixed. This would have failed 100% of runs
even with the file restored — it was never reached because of bug #1.

## 3. Thumbnail was the wrong aspect ratio

`thumbnail()` drew on the raw 1080x1920 frame. YouTube thumbnails are **16:9**;
a vertical image gets letterboxed and your text cropped. Now renders 1280x720
with a blurred-fill background and the sharp frame centered, text moved to the
**top** so it no longer collides with the caption already baked into the video,
plus quality stepping to stay under YouTube's 2 MB limit.

## 4. Freesound returned non-CC0 audio

```python
params={"query": query, "license": "Creative Commons 0", ...}
```

`license` is not a valid Freesound search parameter — it was silently ignored, so
you could have pulled attribution-required or NC audio into a monetized channel.
Correct form is a `filter` query:

```python
params={"query": query, "filter": 'license:"Creative Commons 0" duration:[0.2 TO 4]', ...}
```

The duration bound also stops a 30-second "sfx" from smothering the narration.

## 5. Smaller fixes

| Issue | Fix |
|---|---|
| `os.environ["X"]` raised bare `KeyError` at import | `require()` with a named, readable error |
| `dur()` hard-required `ffprobe` | falls back to parsing `ffmpeg -i` output |
| Thumbnail 403 killed a **successful** upload | caught; video stays live, logs the verify hint |
| Videos uploaded as `unlisted` | `PRIVACY_STATUS` env, defaults `public` |
| `TARGET = 24.0` | `25.0`, as you specified |
| No way to test without uploading | `DRY_RUN=true` + a dry-run checkbox in Actions |
| Concurrent runs could reject the history push | `git pull --rebase --autostash` with 3 retries |
| `thumbnail_text` missing from plan → `KeyError` | defaults to `SO CUTE` |

## Verification

`test_assembly.py` runs the real `bake` → `make_clips` → `mix` → `thumbnail`
code against synthetic images and narration — **no API keys, no network**:

```
$ python test_assembly.py
noaudio.mp4 = 25.10s
final.mp4 = 25.00s  credits=['Music: funny_five (CC-BY/CC0)']
thumb.jpg = 56 KB
ASSEMBLY TEST PASSED
```

Output verified as `1080x1920, 25.00s, h264 High yuv420p, aac 44100 Hz stereo` —
exactly Shorts spec. The Gemini planner was also tested against a mocked API
(including markdown-fenced JSON, which it correctly strips).

I could not test the live Gemini/Pollinations/Kokoro/YouTube calls — those need
your keys. Model URLs and Pollinations were confirmed reachable (HTTP 200).

## Before you re-enable the schedule

1. Push this, then **Actions → Post Animal Short → Run workflow** with
   **dry run ✅**. Confirm it turns green and download the artifact to watch the video.
2. Verify your channel at [youtube.com/verify](https://youtube.com/verify), or custom
   thumbnails get rejected (upload still succeeds; the log tells you).
3. If your OAuth consent screen is still in **Testing**, the refresh token dies after
   7 days — hit **Publish app** to make it permanent.
4. Optional repo variable `PRIVACY_STATUS=unlisted` for the first few runs.
