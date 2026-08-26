#!/usr/bin/env python3
"""
Turn one long video into N fixed-length vertical shorts, each with its own
TTS voiceover and word-timed burned-in captions.

Fully local and unmetered: yt-dlp (download), edge-tts (voice), ffmpeg (video).
No LLM, no transcription, no API keys -- the narration text is supplied by you.

  python make_shorts.py --url "<youtube-url>" --texts texts.json
"""

import argparse
import asyncio
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import edge_tts
import imageio_ffmpeg

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()

# Speeding narration up beyond this to fit the clip makes it sound rushed;
# past it we warn instead of silently mangling the delivery.
MAX_TEMPO = 1.30


# ---------------------------------------------------------------- utilities

def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def probe(path):
    """Return (duration_seconds, has_audio). Parsed from ffmpeg's header;
    imageio-ffmpeg ships ffmpeg but not ffprobe."""
    out = run([FFMPEG, "-hide_banner", "-i", str(path)]).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    dur = 0.0
    if m:
        h, mi, s = m.groups()
        dur = int(h) * 3600 + int(mi) * 60 + float(s)
    return dur, bool(re.search(r"Stream #\d+:\d+.*: Audio:", out))


def hms(t):
    """Seconds -> ASS timestamp H:MM:SS.cc"""
    if t < 0:
        t = 0.0
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def safe_name(text, index):
    slug = re.sub(r"[^\w\s-]", "", text).strip()
    slug = re.sub(r"\s+", "_", slug)[:50].strip("_")
    return f"{index:03d}_{slug or 'clip'}.mp4"


# ------------------------------------------------------------------ texts

def diagnose_download(r):
    """yt-dlp failures on YouTube are nearly always one of a few known causes;
    say which one and what to do rather than echoing the raw log."""
    log = ((r.stderr or "") + (r.stdout or ""))
    tail = log[-1200:]
    hints = []
    if "Sign in to confirm" in log or "not a bot" in log:
        hints.append(
            "YouTube is bot-gating this IP. Pass browser cookies:\n"
            "    --cookies-from-browser safari      (or chrome/firefox/brave/edge)\n"
            "  You must be signed in to YouTube in that browser. For Chrome, quit it\n"
            "  first -- it locks its cookie database while running.")
    if "429" in log or "Too Many Requests" in log:
        hints.append(
            "HTTP 429: too many requests from this IP. Wait a few minutes before\n"
            "  retrying; cookies also make this far less likely.")
    if "JavaScript runtime" in log:
        hints.append(
            "No JS runtime found. Install one:  brew install deno\n"
            "  yt-dlp needs it for YouTube extraction; without it formats go missing.")
    if "Video unavailable" in log or "Private video" in log:
        hints.append("The video is private, region-locked, or removed.")

    msg = "download failed.\n\n" + "\n\n".join(f"  * {h}" for h in hints)
    if not hints:
        msg = "download failed.\n"
    return (f"{msg}\n\nFallback that always works: download the file yourself,\n"
            f"then use --video <file> instead of --url.\n\n"
            f"--- yt-dlp output (tail) ---\n{tail}")


def load_texts(path, count):
    """Accepts {"1": "...", "2": "..."} or ["...", "..."]. Dict keys are
    sorted numerically when they look like numbers, else lexically."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict):
        keys = list(data.keys())
        if all(re.fullmatch(r"\d+", str(k)) for k in keys):
            keys.sort(key=lambda k: int(k))
        else:
            keys.sort()
        texts = [str(data[k]).strip() for k in keys]
    elif isinstance(data, list):
        texts = [str(t).strip() for t in data]
    else:
        sys.exit("texts file must be a JSON object or array")

    texts = [t for t in texts if t]
    if not texts:
        sys.exit("no non-empty texts found")
    if count and len(texts) != count:
        print(f"  note: {len(texts)} texts supplied, --count is {count}; "
              f"using {min(len(texts), count)}")
    return texts[:count] if count else texts


# -------------------------------------------------------------------- tts

# edge-tts >=7 defaults to SentenceBoundary; word-level timings require asking
# for them explicitly. Older releases reject the kwarg entirely, so probe first.
_WANTS_BOUNDARY = "boundary" in inspect.signature(
    edge_tts.Communicate.__init__).parameters


async def _synth(text, voice, mp3_path):
    """Synthesize one line, returning per-word timings from edge-tts.
    These are exact -- no transcription/ASR guesswork involved."""
    kw = {"boundary": "WordBoundary"} if _WANTS_BOUNDARY else {}
    comm = edge_tts.Communicate(text, voice, **kw)
    audio = bytearray()
    words = []
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio.extend(chunk["data"])
        elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
            words.append({
                "start": chunk["offset"] / 1e7,
                "end": (chunk["offset"] + chunk["duration"]) / 1e7,
                "text": chunk["text"],
            })
    if not audio:
        raise RuntimeError("edge-tts returned no audio")
    if not words:
        raise RuntimeError("edge-tts returned no word timings (captions would be empty)")
    Path(mp3_path).write_bytes(bytes(audio))
    return words


def synth_all(items, voice, workdir, jobs):
    """TTS every line up front so a network hiccup fails before any encoding."""
    async def gate():
        sem = asyncio.Semaphore(jobs)

        async def one(it):
            async with sem:
                for attempt in (1, 2, 3):
                    try:
                        it["words"] = await _synth(
                            it["text"], voice, workdir / f"tts_{it['i']:03d}.mp3")
                        return
                    except Exception as e:                      # noqa: BLE001
                        if attempt == 3:
                            it["error"] = str(e)
                            return
                        await asyncio.sleep(1.5 * attempt)

        await asyncio.gather(*(one(it) for it in items))

    asyncio.run(gate())


# -------------------------------------------------------------- subtitles

# ASS colours are &HAABBGGRR -- alpha, then BLUE, GREEN, RED (not RGB).
WHITE, BLACK = "&H00FFFFFF", "&H00000000"
YELLOW, GREEN, CYAN = "&H0000FFFF", "&H0000FF00", "&H00FFFF00"

# Named caption looks. `pos` is percent from the top of a 1920px frame, so 50
# is dead centre. `words` is how many words share one caption card.
STYLES = {
    "bold":    dict(font="Arial Black", size=88, bold=-1, primary=WHITE,  outline_col=BLACK,
                    back="&H80000000", border=1, outline=7, shadow=3,
                    upper=True,  words=3, pos=50.0),
    "small":   dict(font="Arial",       size=54, bold=-1, primary=WHITE,  outline_col=BLACK,
                    back="&H80000000", border=1, outline=3, shadow=1,
                    upper=False, words=6, pos=50.0),
    "yellow":  dict(font="Arial Black", size=88, bold=-1, primary=YELLOW, outline_col=BLACK,
                    back="&H80000000", border=1, outline=7, shadow=3,
                    upper=True,  words=3, pos=50.0),
    "neon":    dict(font="Arial Black", size=84, bold=-1, primary=GREEN,  outline_col=BLACK,
                    back="&H80000000", border=1, outline=6, shadow=2,
                    upper=True,  words=3, pos=50.0),
    "boxed":   dict(font="Arial",       size=62, bold=-1, primary=WHITE,  outline_col=BLACK,
                    back="&HB0000000", border=3, outline=10, shadow=0,
                    upper=False, words=5, pos=50.0),
    "minimal": dict(font="Arial",       size=52, bold=0,  primary=WHITE,  outline_col=BLACK,
                    back="&H80000000", border=1, outline=2, shadow=0,
                    upper=False, words=7, pos=80.0),
}

POSITION_PERCENT = {"top": 18.0, "center": 50.0, "bottom": 80.0}

ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{outline_col},{back},{bold},0,0,0,100,100,0,0,{border},{outline},{shadow},5,{marginlr},{marginlr},60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def resolve_styles(args, n):
    """Return a per-clip list of style dicts.

    Either one style for the whole run (--style), or consecutive blocks
    (--style-plan "bold:10,small:10,yellow:30"). CLI flags override whatever
    the chosen preset specifies.
    """
    if args.style_plan:
        names = []
        for part in args.style_plan.split(","):
            part = part.strip()
            if not part:
                continue
            if ":" not in part:
                sys.exit(f"--style-plan needs name:count entries, got {part!r}")
            name, _, cnt = part.partition(":")
            name = name.strip()
            if name not in STYLES:
                sys.exit(f"unknown style {name!r}; choose from {', '.join(STYLES)}")
            try:
                cnt = int(cnt)
            except ValueError:
                sys.exit(f"--style-plan count must be a number, got {cnt!r}")
            names += [name] * cnt
        if not names:
            sys.exit("--style-plan produced no clips")
        if len(names) < n:
            print(f"      note: style plan covers {len(names)} clips, "
                  f"{n} needed -- remaining use {names[-1]!r}")
            names += [names[-1]] * (n - len(names))
        elif len(names) > n:
            print(f"      note: style plan covers {len(names)} clips but only "
                  f"{n} texts -- extra entries ignored")
        names = names[:n]
    else:
        names = [args.style] * n

    out = []
    for name in names:
        st = dict(STYLES[name])
        st["name"] = name
        if args.font:
            st["font"] = args.font
        if args.font_size:
            st["size"] = args.font_size
        if args.words_per_caption:
            st["words"] = args.words_per_caption
        if args.no_uppercase:
            st["upper"] = False
        if args.position_percent is not None:
            st["pos"] = args.position_percent
        elif args.position:
            st["pos"] = POSITION_PERCENT[args.position]
        st["pos"] = min(96.0, max(4.0, float(st["pos"])))
        out.append(st)
    return out


def write_ass(words, path, style, tempo, limit):
    """Group words into caption cards, timed from their own boundaries.
    `tempo` compensates when the audio was sped up to fit the clip."""
    chunk = max(1, int(style["words"]))
    y = round(style["pos"] / 100.0 * 1920)
    lines = []
    for i in range(0, len(words), chunk):
        grp = words[i:i + chunk]
        start = grp[0]["start"] / tempo
        end = grp[-1]["end"] / tempo
        if start >= limit:
            break
        end = min(end, limit)
        if end <= start:
            continue
        text = " ".join(w["text"] for w in grp)
        text = text.upper() if style["upper"] else text
        # '{' and '}' delimit override tags in ASS; neutralize them.
        text = text.replace("{", "(").replace("}", ")").replace("\n", " ")
        # \an5 + \pos puts the card at an exact height regardless of line count.
        lines.append(
            f"Dialogue: 0,{hms(start)},{hms(end)},Default,,0,0,0,,"
            f"{{\\an5\\pos(540,{y})}}{text}")

    head = ASS_HEAD.format(
        font=style["font"], size=style["size"], primary=style["primary"],
        outline_col=style["outline_col"], back=style["back"], bold=style["bold"],
        border=style["border"], outline=style["outline"], shadow=style["shadow"],
        marginlr=90)
    Path(path).write_text(head + "\n".join(lines) + "\n", encoding="utf-8")
    return len(lines)


# ----------------------------------------------------------------- encode

def build_filter(has_src_audio, ass_name, duck, tempo):
    """Center-crop to 9:16, scale to 1080x1920, burn captions, duck source audio
    under the voiceover."""
    v = (f"[0:v]crop='min(iw,ih*9/16)':ih,scale=1080:1920,"
         f"setsar=1,ass={ass_name}[v]")

    tts = "[1:a]"
    if tempo > 1.001:
        tts += f"atempo={tempo:.4f},"
    tts += "apad[a1]"

    if has_src_audio:
        # duration=first -> the source leg (exactly clip length) sets the end,
        # so padded narration never stretches the clip.
        a = (f"[0:a]volume={duck:.3f}[a0];{tts};"
             f"[a0][a1]amix=inputs=2:duration=first:normalize=0[a]")
    else:
        a = tts.replace("[a1]", "[a]")
    return f"{v};{a}"


def encode(job, src, workdir, duck, encoder):
    ass_name = f"sub_{job['i']:03d}.ass"
    out = job["out"]
    filt = build_filter(job["has_audio"], ass_name, duck, job["tempo"])

    def attempt(enc):
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{job['start']:.3f}", "-t", f"{job['dur']:.3f}", "-i", str(src),
               "-i", f"tts_{job['i']:03d}.mp3",
               "-filter_complex", filt,
               "-map", "[v]", "-map", "[a]",
               "-t", f"{job['dur']:.3f}"]
        if enc == "videotoolbox":
            cmd += ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
        else:
            cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
        cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-ar", "44100", "-ac", "2",
                "-movflags", "+faststart", str(out)]
        return run(cmd, cwd=workdir)

    r = attempt(encoder)
    if r.returncode != 0 and encoder == "videotoolbox":
        # Hardware encoder can refuse odd dimensions/params; software always works.
        r = attempt("libx264")
    if r.returncode != 0:
        return False, (r.stderr or "").strip().splitlines()[-1:] or ["ffmpeg failed"]
    return True, None


# ------------------------------------------------------------------- main

def print_styles():
    print("caption styles (use --style NAME, or --style-plan \"name:count,...\"):\n")
    for name, st in STYLES.items():
        place = ("top" if st["pos"] < 35 else "center" if st["pos"] < 65 else "bottom")
        box = ", boxed background" if st["border"] == 3 else ""
        print(f"  {name:<9} {st['size']}px {st['font']}, "
              f"{st['words']} words/card, {'UPPERCASE' if st['upper'] else 'normal case'}, "
              f"{place} ({st['pos']:.0f}%){box}")
    print("\n  override any of them with --font-size / --words-per-caption /")
    print("  --position top|center|bottom / --position-percent N / --no-uppercase")


def main():
    # Listing styles shouldn't require --texts and a source.
    if "--list-styles" in sys.argv:
        print_styles()
        return

    p = argparse.ArgumentParser(
        description="Long video -> N fixed-length vertical shorts with TTS voiceover.")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--url", help="YouTube (or any yt-dlp supported) URL")
    src.add_argument("--video", help="local source video file")
    p.add_argument("--texts", required=True, help="JSON dict or list of narration texts")
    p.add_argument("--count", type=int, default=None,
                   help="number of shorts (default: however many texts the "
                        "JSON contains)")
    p.add_argument("--duration", type=float, default=30.0,
                   help="fixed seconds per short (default 30)")
    p.add_argument("--start", type=float, default=0.0,
                   help="skip this many seconds of source (default 0)")
    p.add_argument("--voice", default="en-US-AvaNeural")
    p.add_argument("--duck", type=float, default=0.12,
                   help="source audio volume under the voiceover (default 0.12)")
    p.add_argument("--cookies-from-browser", metavar="BROWSER",
                   help="chrome, safari, firefox, brave, edge -- fixes "
                        "'Sign in to confirm you are not a bot'")
    p.add_argument("--cookies", metavar="FILE", help="Netscape-format cookies.txt")
    p.add_argument("--ytdlp-arg", action="append", metavar="ARG",
                   help="extra raw yt-dlp argument (repeatable)")
    p.add_argument("--only", type=int, metavar="N",
                   help="render just clip N, with its real footage offset and "
                        "style -- for previewing settings before a full batch")
    p.add_argument("--outdir", default="output")
    p.add_argument("--workdir", default="work")
    p.add_argument("--jobs", type=int, default=3, help="parallel encodes (default 3)")
    p.add_argument("--encoder", choices=["videotoolbox", "libx264"],
                   default="videotoolbox", help="videotoolbox = M-series hardware")
    p.add_argument("--style", default="bold", choices=sorted(STYLES),
                   help="one caption style for every clip (default bold)")
    p.add_argument("--style-plan", default="",
                   help='per-block styles, e.g. "bold:10,small:10,yellow:30"')
    p.add_argument("--list-styles", action="store_true", help="show styles and exit")
    p.add_argument("--position", choices=sorted(POSITION_PERCENT),
                   help="caption placement (overrides the style's own)")
    p.add_argument("--position-percent", type=float,
                   help="exact caption height, 0=top 100=bottom (overrides --position)")
    # These stay None so a style's own value survives unless explicitly overridden.
    p.add_argument("--words-per-caption", type=int)
    p.add_argument("--font")
    p.add_argument("--font-size", type=int)
    p.add_argument("--no-uppercase", action="store_true")
    p.add_argument("--keep-work", action="store_true")
    args = p.parse_args()

    # No source given -> reuse the cached download, which is the usual case
    # after the first run.
    if not args.url and not args.video:
        cached = Path(args.workdir).resolve() / "source.mp4"
        if cached.exists():
            args.video = str(cached)
        else:
            sys.exit("no --url or --video given, and no cached work/source.mp4 "
                     "to fall back on")

    outdir = Path(args.outdir).resolve()
    workdir = Path(args.workdir).resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)

    texts = load_texts(args.texts, args.count)
    n = len(texts)
    styles = resolve_styles(args, n)

    # ---- source video
    if args.url:
        src_path = workdir / "source.mp4"
        if src_path.exists():
            print(f"[1/5] reusing downloaded source: {src_path}")
        else:
            print(f"[1/5] downloading source with yt-dlp ...")
            ytdlp = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")
            cmd = [ytdlp, "-f", "bv*[height<=1080]+ba/b[height<=1080]/b",
                   "--merge-output-format", "mp4",
                   # yt-dlp merges the separate video/audio streams itself and
                   # only finds ffmpeg on PATH -- ours lives inside the venv.
                   "--ffmpeg-location", FFMPEG,
                   # YouTube rate-limits hard; back off instead of hammering.
                   "--retries", "10", "--retry-sleep", "exp=2:60",
                   "--sleep-requests", "1.5",
                   "-o", str(src_path)]
            if args.cookies_from_browser:
                cmd += ["--cookies-from-browser", args.cookies_from_browser]
            elif args.cookies:
                cmd += ["--cookies", args.cookies]
            if args.ytdlp_arg:
                cmd += args.ytdlp_arg
            cmd.append(args.url)

            r = run(cmd)
            if r.returncode != 0 or not src_path.exists():
                sys.exit(diagnose_download(r))
    else:
        src_path = Path(args.video).resolve()
        if not src_path.exists():
            sys.exit(f"no such file: {src_path}")
        print(f"[1/5] using local source: {src_path}")

    src_dur, has_audio = probe(src_path)
    if src_dur <= 0:
        sys.exit("could not read source duration")
    print(f"      source: {src_dur/60:.1f} min, audio={'yes' if has_audio else 'no'}")

    need = args.start + n * args.duration
    wrap = False
    if need > src_dur:
        wrap = True
        print(f"      ! source is {src_dur/60:.1f} min but {n} x {args.duration:.0f}s "
              f"needs {need/60:.1f} min -- clip positions will wrap around")

    # ---- tts
    items = [{"i": i + 1, "text": t} for i, t in enumerate(texts)]
    if args.only:
        # Preview one clip exactly as it will appear in the full batch: keep its
        # original index so the footage offset and style assignment both match.
        if args.only > len(items):
            sys.exit(f"--only {args.only}, but only {len(items)} texts loaded")
        items = [it for it in items if it["i"] == args.only]
        print(f"      preview mode: clip {args.only} only")
    print(f"[2/5] synthesizing {len(items)} voiceover(s) with edge-tts "
          f"({args.voice}) ...")
    synth_all(items, args.voice, workdir, max(1, args.jobs))
    failed = [it for it in items if it.get("error")]
    for it in failed:
        print(f"      ! clip {it['i']}: TTS failed: {it['error']}")
    items = [it for it in items if not it.get("error")]
    if not items:
        sys.exit("all TTS requests failed -- check network")

    # ---- plan each clip
    print(f"[3/5] planning clips (fixed {args.duration:.0f}s each, 9:16 center crop) ...")
    span = max(1.0, src_dur - args.duration)
    jobs, overlong = [], []
    for it in items:
        idx = it["i"]
        pos = args.start + (idx - 1) * args.duration
        pos = (pos % span) if wrap else min(pos, span)

        mp3 = workdir / f"tts_{idx:03d}.mp3"
        tts_dur, _ = probe(mp3)
        tempo = 1.0
        if tts_dur > args.duration + 0.05:
            tempo = tts_dur / args.duration
            if tempo > MAX_TEMPO:
                overlong.append((idx, tts_dur))
                tempo = MAX_TEMPO
        jobs.append({
            "i": idx, "start": pos, "dur": args.duration, "tempo": tempo,
            "has_audio": has_audio,
            "out": outdir / safe_name(it["text"], idx),
        })
        write_ass(it["words"], workdir / f"sub_{idx:03d}.ass",
                  styles[idx - 1], tempo, args.duration)

    # ---- encode
    print(f"[4/5] encoding {len(jobs)} shorts ({args.encoder}, {args.jobs} parallel) ...")
    done, errs = 0, []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as ex:
        futs = {ex.submit(encode, j, src_path, workdir, args.duck, args.encoder): j
                for j in jobs}
        for f in futs:
            pass
        for f, j in futs.items():
            ok, err = f.result()
            done += ok
            if not ok:
                errs.append((j["i"], err))
            print(f"      {done + len(errs)}/{len(jobs)}", end="\r", flush=True)

    # ---- report
    used = {}
    for st in styles:
        used[st["name"]] = used.get(st["name"], 0) + 1
    print(f"\n[5/5] done: {done} videos in {outdir}")
    print("      styles: " + ", ".join(f"{k} x{v}" for k, v in used.items()))
    if overlong:
        print(f"      ! {len(overlong)} text(s) too long for a "
              f"{args.duration:.0f}s clip even at {MAX_TEMPO}x speed -- narration "
              f"is cut off at the end. Shorten them or raise --duration:")
        for idx, d in overlong[:10]:
            print(f"        clip {idx}: needs ~{d:.1f}s")
    for idx, err in errs:
        print(f"      ! clip {idx} failed: {err}")
    if failed:
        print(f"      ! {len(failed)} clip(s) skipped due to TTS failure")
    if not args.keep_work:
        for pat in ("tts_*.mp3", "sub_*.ass"):
            for f in workdir.glob(pat):
                f.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
