#!/usr/bin/env python3
"""Plan -> images -> narration -> SFX -> music -> 25s MP4 -> YouTube. Costs $0."""
import json, os, random, sqlite3, subprocess, time
from pathlib import Path
import requests
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1080, 1920, 30
TARGET = 25.0                                # target length in seconds
OUT = Path("output"); OUT.mkdir(exist_ok=True)
DB  = Path("data/history.db")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DRY_RUN    = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
PRIVACY    = os.environ.get("PRIVACY_STATUS", "public")
GEMINI     = os.environ.get("GEMINI_API_KEY", "")
FS_KEY     = os.environ.get("FREESOUND_API_KEY", "")
YT_ID      = os.environ.get("YOUTUBE_CLIENT_ID", "")
YT_SEC     = os.environ.get("YOUTUBE_CLIENT_SECRET", "")
YT_REFRESH = os.environ.get("YOUTUBE_REFRESH_TOKEN", "")
VOICE      = os.environ.get("KOKORO_VOICE", "af_heart")

def require(name, value):
    if not value:
        raise SystemExit(f"Missing required env var / GitHub secret: {name}")
    return value

def sh(*cmd):
    """Run a command, and on failure show the tail of stderr instead of hiding it."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"command failed: {' '.join(map(str, cmd))[:300]}\n{r.stderr[-1500:]}")
    return r

def _ffmpeg_exe():
    from shutil import which
    if which("ffmpeg"):
        return "ffmpeg"
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def dur(p):
    """Duration in seconds. Uses ffprobe when available, else parses ffmpeg output."""
    from shutil import which
    if which("ffprobe"):
        out = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
            "-of","csv=p=0",str(p)], capture_output=True, text=True).stdout.strip()
        if out:
            return float(out)
    err = subprocess.run([_ffmpeg_exe(),"-i",str(p)], capture_output=True, text=True).stderr
    for line in err.splitlines():
        if "Duration:" in line:
            hms = line.split("Duration:")[1].split(",")[0].strip()
            h, m, sec = hms.split(":")
            return int(h)*3600 + int(m)*60 + float(sec)
    raise RuntimeError(f"could not determine duration of {p}")

def db():
    DB.parent.mkdir(exist_ok=True)
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS videos(ts,title,idea,vid,status)")
    c.commit()
    return c

# ---------- 1. PLAN (Gemini free tier) ----------
PLAN_PROMPT = """You are the creative director of a family-friendly YouTube Shorts channel
of funny ANIMATED animal stories (bright 3D-cartoon style).
Create ONE new ~25-second vertical video concept.
Previously used titles (avoid anything similar):
{recent}

Rules:
- One funny animal character; harmless cute comedy; strong hook in scene 1; funny payoff at the end.
- Exactly 5 scenes. Each scene has: image_prompt, narration (1-2 sentences),
  caption (max 5 words, NO emoji), sfx_query (1-3 words, e.g. "cartoon boing").
- Every image_prompt must START with the exact same character description from the
  "character" field, then the scene, then "vertical 9:16, cute 3D animated cartoon,
  vibrant colors, expressive face".
- Total narration 55-70 words, playful tone.
- Title under 70 chars, catchy, max one emoji.
- description: 2 sentences. tags: 10-15. thumbnail_text: max 4 words UPPERCASE.
Return ONLY valid JSON:
{{"idea":"...","character":"...","title":"...","description":"...","tags":["..."],
"thumbnail_text":"...",
"scenes":[{{"image_prompt":"...","narration":"...","caption":"...","sfx_query":"..."}}]}}"""

def make_plan(recent):
    prompt = PLAN_PROMPT.format(
        recent="\n".join(recent) or "(none)"
    )

    # First discover which models this API key can actually use.
    model_response = requests.get(
        f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI}",
        timeout=60
    )

    try:
        model_data = model_response.json()
    except Exception:
        raise SystemExit(
            f"Gemini model-list request failed: HTTP {model_response.status_code}: "
            f"{model_response.text[:1000]}"
        )

    if "error" in model_data:
        raise SystemExit(
            "Gemini API error while listing models: "
            + json.dumps(model_data["error"], indent=2)
        )

    available = []

    for model in model_data.get("models", []):
        methods = model.get("supportedGenerationMethods", [])
        if "generateContent" in methods:
            name = model.get("name", "").replace("models/", "")
            if name:
                available.append(name)

    print("Available Gemini generateContent models:")
    print(available)

    # Prefer current text Flash models. Older 2.x names still appear in the model
    # list but return 404 "no longer available to new users" for new API keys,
    # so newest-first ordering matters here.
    preferred = [
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-3-flash-preview",
        "gemini-flash-latest",
        "gemini-3.5-flash-lite",
        "gemini-3.1-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-2.5-flash",
    ]

    def is_text_flash(m):
        low = m.lower()
        bad = ("image", "tts", "vision", "video", "robotics", "computer-use",
               "deep-research", "lyria", "omni", "embedding", "eap")
        return "flash" in low and not any(b in low for b in bad)

    models = [m for m in preferred if m in available]
    # append any other usable flash models as extra fallbacks
    models += [m for m in available if is_text_flash(m) and m not in models]
    if not models:
        models = [m for m in available if "pro" in m.lower()
                  and "image" not in m.lower() and "preview" not in m.lower()][:2]

    if not models:
        raise SystemExit(
            "Your Gemini key has no compatible generateContent Flash model."
        )

    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}]
            }
        ],
        "generationConfig": {
            "temperature": 1.0,
            "responseMimeType": "application/json"
        }
    }

    for model in models:
        print(f"Trying Gemini model: {model}")

        for attempt in range(3):
            try:
                response = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/"
                    f"{model}:generateContent?key={GEMINI}",
                    json=body,
                    timeout=90
                )

                print(f"Gemini HTTP status: {response.status_code}")

                # 404 = model retired for this key: no point retrying, next model
                if response.status_code == 404:
                    print(f"{model} not available to this key, skipping")
                    break

                # 503/429 = transient overload or rate limit: back off and retry
                if response.status_code in (429, 503, 500, 502, 504):
                    wait = 15 * (attempt + 1)
                    print(f"transient {response.status_code}, retrying in {wait}s "
                          f"({attempt+1}/3)")
                    time.sleep(wait)
                    continue

                try:
                    data = response.json()
                except Exception:
                    print("Non-JSON Gemini response:")
                    print(response.text[:2000])
                    break

                if "error" in data:
                    print(
                        "Gemini API error:",
                        json.dumps(data["error"], indent=2)
                    )
                    break

                if "candidates" not in data:
                    print(
                        "Gemini returned no candidates:",
                        json.dumps(data, indent=2)[:3000]
                    )
                    break

                raw = data["candidates"][0]["content"]["parts"][0]["text"]

                raw = raw.strip()

                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1]
                    raw = raw.rsplit("```", 1)[0].strip()

                plan = json.loads(raw)

                if len(plan.get("scenes", [])) == 5:
                    print(f"Planner succeeded with {model}")
                    return plan

                print("Gemini JSON did not contain exactly 5 scenes.")
                break

            except Exception as e:
                print(f"Planner exception using {model}: {repr(e)}")
                break

    raise SystemExit("planner failed — see Gemini diagnostics above")

# ---------- 2. IMAGES (Pollinations, free, no key) ----------
def image(prompt, seed, path):
    url = ("https://image.pollinations.ai/prompt/" + requests.utils.quote(prompt, safe="")
           + f"?width={W}&height={H}&seed={seed}&nologo=true")
    for a in range(4):
        try:
            r = requests.get(url, timeout=240)
            if r.ok and r.headers.get("content-type","").startswith("image"):
                path.write_bytes(r.content); return
        except Exception as e:
            print("image retry:", e)
        time.sleep(15*(a+1))
    raise SystemExit(f"image failed: {path.name}")

# ---------- 3. CAPTIONS baked onto frames ----------
def bake(src, dst, caption):
    img = Image.open(src).convert("RGB"); d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 68); lines, cur = [], ""
    for w_ in caption.split():
        t = (cur+" "+w_).strip()
        if d.textlength(t, font=f) > W-160: lines.append(cur); cur = w_
        else: cur = t
    lines.append(cur)
    y = H - 300 - len(lines)*84
    for ln in lines:
        d.text(((W-d.textlength(ln,font=f))/2, y), ln, font=f,
               fill="white", stroke_width=8, stroke_fill="black"); y += 84
    img.save(dst, quality=92)

# ---------- 4. NARRATION (Kokoro, local) ----------
def narrate(text, path):
    from kokoro_onnx import Kokoro
    import soundfile as sf
    k = Kokoro("models/kokoro-v1.0.onnx", "models/voices-v1.0.bin")
    a, sr = k.create(text, voice=VOICE, speed=1.0, lang="en-us")
    sf.write(path, a, sr)

# ---------- 5. SFX (Freesound CC0 previews, free key) ----------
def sfx(query, path):
    if not FS_KEY or not query: return False
    try:
        r = requests.get("https://freesound.org/apiv2/search/text/",
            params={"query":query,
                    "filter":'license:"Creative Commons 0" duration:[0.2 TO 4]',
                    "fields":"previews","page_size":5,"token":FS_KEY}, timeout=30).json()
        for s in r.get("results", []):
            u = s["previews"].get("preview-hq-mp3")
            if u:
                b = requests.get(u, timeout=30).content
                if len(b) > 10000: path.write_bytes(b); return True
    except Exception as e:
        print("sfx skip:", e)
    return False

# ---------- 6. ASSEMBLE ----------
def make_clips(plan, starts, video_dur):
    clips = []
    for i, sc in enumerate(plan["scenes"]):
        d = (starts[i+1] if i+1 < len(starts) else video_dur) - starts[i]
        frames = int(d*FPS)+1
        baked = OUT/f"baked{i}.jpg"; bake(OUT/f"img{i}.jpg", baked, sc["caption"])
        xf = f"(iw-iw/zoom)*(on/{frames})" if i%2==0 else f"(iw-iw/zoom)*(1-on/{frames})"
        vf = (f"scale={W*2}:{H*2},zoompan=z='min(zoom+0.0007,1.18)':x='{xf}':"
              f"y='(ih-ih/zoom)/2':d={frames}:s={W}x{H}:fps={FPS},format=yuv420p")
        clip = OUT/f"clip{i}.mp4"
        sh("ffmpeg","-y","-i",str(baked),"-vf",vf,"-frames:v",str(frames),
           "-c:v","libx264","-preset","veryfast","-crf","20",str(clip))
        clips.append(clip)
    (OUT/"clips.txt").write_text("".join(f"file '{c.name}'\n" for c in clips))
    sh("ffmpeg","-y","-f","concat","-safe","0","-i",str(OUT/"clips.txt"),
       "-c","copy",str(OUT/"noaudio.mp4"))

def mix(plan, starts, video_dur, atempo):
    # NOTE: input 0 is noaudio.mp4 (video), so audio inputs are numbered from 1.
    inputs, filters, labels, n, credits = [], [], [], 1, []
    FMT = "aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo"
    inputs += ["-i", str(OUT/"narration.wav")]
    filters.append(f"[{n}:a]atempo={atempo:.3f},{FMT}[nar]"); labels.append("[nar]"); n += 1
    for i, sc in enumerate(plan["scenes"]):
        p = OUT/f"sfx{i}.mp3"
        if sfx(sc.get("sfx_query",""), p):
            ms = int(starts[i]*1000)
            inputs += ["-i", str(p)]
            filters.append(f"[{n}:a]adelay={ms}|{ms},volume=1.1,{FMT}[s{i}]")
            labels.append(f"[s{i}]"); n += 1
            credits.append(f"SFX: freesound.org (CC0)")
    tracks = sorted(Path("assets/music").glob("*.mp3")) if Path("assets/music").exists() else []
    if tracks:
        m = random.choice(tracks)
        inputs += ["-stream_loop","-1","-i",str(m)]
        filters.append(f"[{n}:a]atrim=0:{video_dur:.2f},"
                       f"afade=t=out:st={video_dur-2:.2f}:d=2,volume=0.18,{FMT}[mus]")
        labels.append("[mus]"); n += 1
        credits.append(f"Music: {m.stem} (CC-BY/CC0)")
    graph = "".join(labels) + (f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                               f"loudnorm=I=-16:TP=-1.5[aout]")
    subprocess.run(["ffmpeg","-y","-i",str(OUT/"noaudio.mp4")] + inputs +
                   ["-filter_complex",";".join(filters+[graph]),
                    "-map","0:v","-map","[aout]","-c:v","copy",
                    "-c:a","aac","-b:a","192k","-ar","44100","-shortest",
                    str(OUT/"final.mp4")], check=True)
    return credits

def thumbnail(plan, starts):
    ts = (starts[1] + 0.5) if len(starts) > 2 else 0.8   # frame from the "reaction" scene
    sh("ffmpeg","-y","-ss",f"{ts:.2f}","-i",str(OUT/"final.mp4"),
       "-frames:v","1",str(OUT/"thumb_raw.jpg"))
    # YouTube wants 16:9 (1280x720). Build blurred-fill background + sharp centered frame,
    # otherwise a 9:16 image gets letterboxed and the text is cropped off.
    from PIL import ImageFilter, ImageEnhance
    TW, TH = 1280, 720
    src = Image.open(OUT/"thumb_raw.jpg").convert("RGB")
    bg = src.resize((TW, TH)).filter(ImageFilter.GaussianBlur(30))
    bg = ImageEnhance.Brightness(bg).enhance(0.55)
    r = min(TW/src.width, TH/src.height)
    fg = src.resize((int(src.width*r), int(src.height*r)), Image.LANCZOS)
    fg = ImageEnhance.Color(fg).enhance(1.2)
    bg.paste(fg, ((TW-fg.width)//2, (TH-fg.height)//2))
    img = bg; d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 118); lines, cur = [], ""
    for w_ in str(plan.get("thumbnail_text") or "SO CUTE").upper().split():
        t = (cur+" "+w_).strip()
        if d.textlength(t, font=f) > TW-100: lines.append(cur); cur = w_
        else: cur = t
    lines.append(cur)
    lines = [l for l in lines if l]
    y = 40   # top: the video's own baked caption sits low, avoid overlapping it
    for ln in lines:
        d.text(((TW-d.textlength(ln,font=f))/2, y), ln, font=f,
               fill="yellow", stroke_width=12, stroke_fill="black"); y += 128
    q = 90
    img.save(OUT/"thumb.jpg", quality=q)
    while (OUT/"thumb.jpg").stat().st_size > 2_000_000 and q > 40:
        q -= 10
        img.save(OUT/"thumb.jpg", quality=q)

# ---------- 7. UPLOAD (YouTube Data API, official + free) ----------
def upload(plan, credits):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    cred = Credentials(None, client_id=YT_ID, client_secret=YT_SEC,
                       refresh_token=YT_REFRESH,
                       token_uri="https://oauth2.googleapis.com/token",
                       scopes=["https://www.googleapis.com/auth/youtube.upload",
                               "https://www.googleapis.com/auth/youtube.force-ssl"])
    cred.refresh(Request())
    yt = build("youtube","v3",credentials=cred)
    body = {"snippet":{"title":plan["title"],
                       "description":plan["description"]+"\n\n"+"\n".join(credits),
                       "tags":plan["tags"],"categoryId":"15"},
            "status":{"privacyStatus":PRIVACY,"selfDeclaredMadeForKids":False}}
    req = yt.videos().insert(part="snippet,status", body=body,
        media_body=MediaFileUpload(str(OUT/"final.mp4"), chunksize=1<<20,
                                   resumable=True, mimetype="video/mp4"))
    resp = None
    while resp is None:
        s, resp = req.next_chunk()
        if s: print(f"upload {int(s.progress()*100)}%")
    try:
        yt.thumbnails().set(videoId=resp["id"],
            media_body=MediaFileUpload(str(OUT/"thumb.jpg"),
                                       mimetype="image/jpeg")).execute()
        print("thumbnail set")
    except Exception as e:
        # custom thumbnails require a verified channel; the video is already live
        print("thumbnail rejected (verify channel at youtube.com/verify):", e)
    return resp["id"]

# ---------- MAIN ----------
def main():
    require("GEMINI_API_KEY", GEMINI)
    if not DRY_RUN:
        require("YOUTUBE_CLIENT_ID", YT_ID)
        require("YOUTUBE_CLIENT_SECRET", YT_SEC)
        require("YOUTUBE_REFRESH_TOKEN", YT_REFRESH)
    con = db()
    recent = [r[0] for r in con.execute(
        "SELECT title FROM videos ORDER BY rowid DESC LIMIT 30")]
    plan = make_plan(recent)
    print("idea:", plan["idea"], "| title:", plan["title"])

    seed = random.randint(1, 10**6); char = plan.get("character","")
    for i, sc in enumerate(plan["scenes"]):
        image(f"{char} {sc['image_prompt']}", seed+i, OUT/f"img{i}.jpg")

    narrate(" ".join(s["narration"] for s in plan["scenes"]), OUT/"narration.wav")
    nd = dur(OUT/"narration.wav")
    atempo = max(1.0, min(nd/(TARGET-1.2), 1.18))          # gently speed narration to fit
    video_dur = min(max(TARGET, nd/atempo + 1.2), 29.0)

    weights = [len(s["narration"].split()) or 1 for s in plan["scenes"]]
    tot = sum(weights); starts, t = [], 0.0
    for w_ in weights:
        starts.append(t); t += w_/tot*video_dur
    starts.append(video_dur)

    make_clips(plan, starts, video_dur)
    credits = mix(plan, starts, video_dur, atempo)
    thumbnail(plan, starts)
    print(f"final video: {dur(OUT/'final.mp4'):.2f}s")

    if DRY_RUN:
        con.execute("INSERT INTO videos VALUES (datetime('now'),?,?,?,'dry_run')",
                    (plan["title"], plan["idea"], ""))
        con.commit()
        print("DRY_RUN: built output/final.mp4 + output/thumb.jpg, nothing uploaded")
        return

    vid = upload(plan, credits)
    con.execute("INSERT INTO videos VALUES (datetime('now'),?,?,?,'published')",
                (plan["title"], plan["idea"], vid))
    con.commit()
    print("DONE: https://youtube.com/shorts/" + vid)

if __name__ == "__main__":
    main()
