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

def sh(*cmd): subprocess.run(cmd, check=True, capture_output=True)

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
    body = {"contents":[{"role":"user","parts":[{"text":PLAN_PROMPT.format(
                recent="\n".join(recent) or "(none)")}]}],
            "generationConfig":{"temperature":1.1,"responseMimeType":"application/json"}}
    for _ in range(4):
        try:
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"gemini-2.0-flash:generateContent?key={GEMINI}",
                json=body, timeout=90).json()
            plan = json.loads(r["candidates"][0]["content"]["parts"][0]["text"])
            if len(plan["scenes"]) >= 4: return plan
        except Exception as e:
            print("planner retry:", e)
        time.sleep(8)
    raise SystemExit("planner failed")

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
            params={"query":query,"license":"Creative Commons 0",
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
    inputs, filters, labels, n, credits = [], [], [], 0, []
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
    img = Image.open(OUT/"thumb_raw.jpg").convert("RGB"); d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 150); lines, cur = [], ""
    for w_ in plan["thumbnail_text"].upper().split():
        t = (cur+" "+w_).strip()
        if d.textlength(t, font=f) > W-140: lines.append(cur); cur = w_
        else: cur = t
    lines.append(cur)
    y = 200
    for ln in lines:
        d.text(((W-d.textlength(ln,font=f))/2, y), ln, font=f,
               fill="yellow", stroke_width=14, stroke_fill="black"); y += 180
    img.save(OUT/"thumb.jpg", quality=90)

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
            "status":{"privacyStatus":"unlisted","selfDeclaredMadeForKids":False}}
    req = yt.videos().insert(part="snippet,status", body=body,
        media_body=MediaFileUpload(str(OUT/"final.mp4"), chunksize=1<<20,
                                   resumable=True, mimetype="video/mp4"))
    resp = None
    while resp is None:
        s, resp = req.next_chunk()
        if s: print(f"upload {int(s.progress()*100)}%")
    yt.thumbnails().set(videoId=resp["id"],
        media_body=MediaFileUpload(str(OUT/"thumb.jpg"),
                                   mimetype="image/jpeg")).execute()
    return resp["id"]

# ---------- MAIN ----------
def main():
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
    vid = upload(plan, credits)
    con.execute("INSERT INTO videos VALUES (datetime('now'),?,?,?,'published')",
                (plan["title"], plan["idea"], vid))
    con.commit()
    print("DONE: https://youtube.com/shorts/" + vid)

if __name__ == "__main__":
    main()
