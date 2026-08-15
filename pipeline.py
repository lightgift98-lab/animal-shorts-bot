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
# Optional real AI video. Without a key the bot uses the free ffmpeg motion engine.
POLLEN_KEY = os.environ.get("POLLINATIONS_API_KEY", "")
VIDEO_MODEL = os.environ.get("VIDEO_MODEL", "wan-fast")
AI_VIDEO   = os.environ.get("AI_VIDEO", "auto").lower()   # auto | always | never

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
of funny REAL animal moments that look like genuine wildlife/pet footage.
Absolutely NOT cartoon, NOT 3D render, NOT illustration - these must read as
real photographs of real animals caught on camera.
Create ONE new ~25-second vertical video concept.
Previously used titles (avoid anything similar):
{recent}

Rules:
- One funny animal character; harmless cute comedy; strong hook in scene 1; funny payoff at the end.
- Exactly 5 scenes. Each scene has: image_prompt, narration (1-2 sentences),
  caption (max 5 words, NO emoji), sfx_query (1-3 words, e.g. "cartoon boing").
- Every image_prompt must START with the exact same character description from the
  "character" field, then the scene. Do NOT append style words yourself; the
  pipeline adds the photographic style suffix.
- The "character" field must describe a REAL animal with specific, repeatable
  physical detail (species, age, exact coat/marking colors, eye color, one
  distinguishing feature) so the same animal is recognisable in all 5 scenes.
  Example: "a small ginger tabby kitten with a white chest patch, one folded left
  ear and bright green eyes".
- Scenes must be physically plausible for a real animal - no talking, no props a
  real pet could not interact with, no impossible physics.
- IMPORTANT - each image_prompt must describe ONE clear pose plus ONE camera
  framing, and nothing else. The image model reliably renders the animal, its
  pose and the shot type, but ignores complicated multi-object interactions.
  Do NOT ask for the animal manipulating objects (knocking over a jar, opening a
  door, treats scattering). Instead vary POSE and CAMERA across the 5 scenes:
  e.g. "extreme close-up of only its eyes peeking over a table edge, low angle",
  "full body mid-air leap, side view, motion blur background",
  "close-up of one paw raised toward the camera, head tilted",
  "curled up asleep in a basket, seen from directly above, top-down".
  Tell the STORY in the narration and captions; let the images carry mood.
- The 5 image_prompts must use 5 DIFFERENT framings (extreme close-up, full body,
  low angle, top-down, side profile) so the video does not look repetitive.
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

# Photographic style suffix. Camera/lens language pushes the model toward a real
# photograph; the negative words suppress the illustrated look.
# Kept deliberately SHORT. A long style block drowns out the scene description and
# the model returns a generic portrait instead of the requested shot.
PHOTO_STYLE = (
    "candid pet photograph, real animal, realistic detailed fur, shallow depth of "
    "field, natural light, 8k, not a cartoon, not a 3d render, no text"
)


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
def caption_png(caption, path):
    """Render the caption to a transparent PNG.

    It must be composited AFTER the parallax, not baked into the source image:
    the still is split into two depth layers that move at different speeds, so
    text baked in beforehand gets duplicated and ghosts apart on screen.
    """
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    size = max(34, int(W * 0.063))
    f = ImageFont.truetype(FONT, size)
    line_h = int(size * 1.24)
    stroke = max(3, size // 9)

    lines, cur = [], ""
    for w_ in str(caption or "").split():
        t = (cur + " " + w_).strip()
        if d.textlength(t, font=f) > W - int(W * 0.15):
            if cur: lines.append(cur)
            cur = w_
        else:
            cur = t
    if cur: lines.append(cur)
    if not lines:
        img.save(path); return path

    y = int(H * 0.80) - len(lines) * line_h
    y = max(int(H * 0.05), min(y, H - len(lines) * line_h - int(H * 0.04)))
    for ln in lines:
        d.text(((W - d.textlength(ln, font=f)) / 2, y), ln, font=f,
               fill=(255, 255, 255, 255), stroke_width=stroke,
               stroke_fill=(0, 0, 0, 255))
        y += line_h
    img.save(path)
    return path


def bake(src, dst, caption):
    """Draw the caption onto the frame.

    Pollinations ignores the requested width/height and returns whatever it
    likes (e.g. 576x1024), so everything here is derived from the ACTUAL image
    size. Using the global W/H put the text far below the canvas and it was
    silently dropped.
    """
    img = Image.open(src).convert("RGB")
    if img.size != (W, H):
        img = img.resize((W, H), Image.LANCZOS)   # normalise to 9:16 1080x1920
    iw, ih = img.size
    d = ImageDraw.Draw(img)

    size = max(34, int(iw * 0.063))               # ~68px at 1080 wide
    f = ImageFont.truetype(FONT, size)
    line_h = int(size * 1.24)
    stroke = max(3, size // 9)

    lines, cur = [], ""
    for w_ in str(caption or "").split():
        t = (cur + " " + w_).strip()
        if d.textlength(t, font=f) > iw - int(iw * 0.15):
            if cur: lines.append(cur)
            cur = w_
        else:
            cur = t
    if cur: lines.append(cur)
    if not lines:
        img.save(dst, quality=92); return

    # sit the block in the lower third, always inside the frame
    y = int(ih * 0.80) - len(lines) * line_h
    y = max(int(ih * 0.05), min(y, ih - len(lines) * line_h - int(ih * 0.04)))
    for ln in lines:
        d.text(((iw - d.textlength(ln, font=f)) / 2, y), ln, font=f,
               fill="white", stroke_width=stroke, stroke_fill="black")
        y += line_h
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
def _motion_mask(path):
    """Static soft-oval alpha mask, built once and reused.

    Doing this in PIL instead of ffmpeg's geq is ~11x faster (5s vs 55s per clip).
    """
    from PIL import ImageFilter
    if path.exists():
        return path
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).ellipse(
        [int(W*0.06), int(H*0.28), int(W*0.94), int(H*0.86)], fill=255)
    m.filter(ImageFilter.GaussianBlur(70)).save(path)
    return path


def animate(src, dst, dur, idx, cap_png=None):
    """Turn a still into a genuinely moving shot.

    Two depth layers: a slow, blurred background pushing one way and a masked
    foreground subject pushing harder the other way. That parallax offset is
    what reads as real motion rather than a flat Ken Burns zoom. Adds a slow
    handheld sway and, on some scenes, drifting particles.
    """
    f = max(2, int(dur * FPS))
    mask = _motion_mask(OUT / "_mask.png")

    # alternate the push direction per scene so cuts feel choreographed
    d = 1 if idx % 2 == 0 else -1
    bg_z = f"1.04+0.09*(on/{f})"
    fg_z = f"1.10+0.24*(on/{f})"
    bg_x = f"iw/2-(iw/zoom/2)-{28*d}*(on/{f})"
    fg_x = f"iw/2-(iw/zoom/2)+{78*d}*(on/{f})"
    fg_y = f"ih/2-(ih/zoom/2)+30*sin(2*PI*on/{f})"     # gentle bob

    chain = (
        f"[0:v]scale={W}:{H},setsar=1,split=2[a][b];"
        f"[a]scale=2200:-1,zoompan=z='{bg_z}':x='{bg_x}':y='ih/2-(ih/zoom/2)':"
        f"d={f}:s={W}x{H}:fps={FPS},gblur=sigma=4[bg];"
        f"[b]scale=2500:-1,zoompan=z='{fg_z}':x='{fg_x}':y='{fg_y}':"
        f"d={f}:s={W}x{H}:fps={FPS}[fgc];"
        f"[1:v]scale={W}:{H},format=gray,fps={FPS}[mk];"
        f"[fgc][mk]alphamerge[fga];"
        f"[bg][fga]overlay=0:0[comp];"
    )

    if idx % 2 == 1:                                   # particles on alternate scenes
        # Both blend inputs must be forced to the same RGB format. Without this the
        # filter inherits the noise source's 'gray' format and the result loses
        # its colour channels (frames came out bright magenta).
        chain += (
            f"[comp]format=gbrp[compc];"
            f"nullsrc=s={W}x{H}:d={dur:.2f}:r={FPS},format=gray,"
            f"geq=lum='if(lt(random(1)*340,1),255,0)',boxblur=2:1,format=gbrp[snow];"
            f"[compc][snow]blend=all_mode=screen:all_opacity=0.45,format=yuv420p[lit];"
            f"[lit]"
        )
    else:
        chain += "[comp]"

    # slow handheld sway, then crop the wobble margin away
    chain += (
        f"rotate='0.006*sin(2*PI*t/5)':ow=iw:oh=ih,"
        f"crop=iw*0.94:ih*0.94,scale={W}:{H},setsar=1,format=yuv420p[v]"
    )

    args = ["ffmpeg", "-y", "-loop", "1", "-i", str(src),
            "-loop", "1", "-i", str(mask)]
    if cap_png:
        # composite the caption on top of the finished motion so it stays rock steady
        args += ["-loop", "1", "-i", str(cap_png)]
        chain = chain.replace("[v]", "[mv];") + \
            f"[2:v]scale={W}:{H},fps={FPS}[cap];[mv][cap]overlay=0:0:format=auto[v]"
    args += ["-t", f"{dur:.3f}", "-filter_complex", chain, "-map", "[v]",
             "-frames:v", str(f), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", str(dst)]
    sh(*args)
    return dst


def ai_clip(prompt, out, dur, start_img=None):
    """Generate a real AI-animated clip via Pollinations.

    Returns None on any failure so the caller can fall back to the free motion
    engine - a paid outage must never take the channel down.
    """
    if not POLLEN_KEY:
        return None
    try:
        params = {"key": POLLEN_KEY, "model": VIDEO_MODEL,
                  "duration": int(max(4, min(10, round(dur)))),
                  "aspectRatio": "9:16"}
        if start_img:
            params["image[0]"] = str(start_img)
        r = requests.get("https://gen.pollinations.ai/video/" +
                         requests.utils.quote(prompt, safe=""),
                         params=params, timeout=600)
        ct = r.headers.get("content-type", "")
        if r.ok and ct.startswith("video"):
            out.write_bytes(r.content)
            print(f"  ai_clip ok ({len(r.content)//1024} KB, {VIDEO_MODEL})")
            return out
        print(f"  ai_clip failed: HTTP {r.status_code} {r.text[:200]}")
    except Exception as e:
        print(f"  ai_clip exception: {e!r}")
    return None


TRANSITIONS = ["smoothleft", "smoothright", "fadeblack", "wipeleft", "circleopen"]
XF = 0.35        # transition length in seconds


def make_clips(plan, starts, video_dur):
    """Animate every scene, then join them with real transitions.

    Clips are cut XF longer than their slot because each xfade consumes XF
    seconds of overlap; without that padding the video ends up short.
    """
    clips = []
    n = len(plan["scenes"])
    for i, sc in enumerate(plan["scenes"]):
        slot = (starts[i+1] if i+1 < len(starts) else video_dur) - starts[i]
        d = slot + (XF if i < n-1 else 0)
        # normalise the still to 1080x1920 (Pollinations returns 576x1024)
        frame = OUT/f"frame{i}.jpg"
        Image.open(OUT/f"img{i}.jpg").convert("RGB").resize((W, H), Image.LANCZOS).save(frame, quality=94)
        cap = caption_png(sc.get("caption", ""), OUT/f"cap{i}.png")
        clip = OUT/f"clip{i}.mp4"

        made = None
        if AI_VIDEO in ("auto", "always") and POLLEN_KEY:
            raw = ai_clip(sc.get("image_prompt", ""), OUT/f"ai{i}.mp4", d)
            if raw:
                # conform the AI clip to our canvas and stamp the caption on
                sh("ffmpeg", "-y", "-i", str(raw), "-loop", "1", "-i", str(cap),
                   "-t", f"{d:.3f}", "-filter_complex",
                   f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                   f"crop={W}:{H},fps={FPS}[b];"
                   f"[1:v]scale={W}:{H},fps={FPS}[c];[b][c]overlay=0:0[v]",
                   "-map", "[v]", "-an", "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "20", "-pix_fmt", "yuv420p", str(clip))
                made = clip
        if made is None:
            if AI_VIDEO == "always" and POLLEN_KEY:
                raise RuntimeError(f"AI_VIDEO=always but scene {i} could not be generated")
            made = animate(frame, clip, d, i, cap)
        clips.append(made)

    if len(clips) == 1:
        sh("ffmpeg", "-y", "-i", str(clips[0]), "-c", "copy", str(OUT/"noaudio.mp4"))
        return

    # chain xfades. Each clip is XF longer than its slot, and every xfade eats
    # XF of overlap, so offset_i = offset_{i-1} + dur_{i-1} - XF. Getting this
    # wrong silently shortens the video (25.0s -> 23.97s).
    inputs, filt, prev = [], [], "[0:v]"
    for c in clips:
        inputs += ["-i", str(c)]
    durs = [(starts[i+1] if i+1 < len(starts) else video_dur) - starts[i]
            + (XF if i < len(clips)-1 else 0) for i in range(len(clips))]
    off = durs[0] - XF
    for i in range(1, len(clips)):
        lbl = f"[x{i}]"
        filt.append(f"{prev}[{i}:v]xfade=transition={TRANSITIONS[(i-1) % len(TRANSITIONS)]}"
                    f":duration={XF}:offset={max(0.1, off):.3f}{lbl}")
        prev = lbl
        if i < len(clips) - 1:
            off += durs[i] - XF
    filt.append(f"{prev}fps={FPS},setsar=1,format=yuv420p[v]")

    sh("ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filt),
       "-map", "[v]", "-t", f"{video_dur:.3f}",
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
       "-pix_fmt", "yuv420p", str(OUT/"noaudio.mp4"))


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
        image(f"{char} {sc['image_prompt']}. {PHOTO_STYLE}", seed+i, OUT/f"img{i}.jpg")

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
