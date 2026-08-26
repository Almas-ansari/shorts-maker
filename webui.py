"""Streamlit front-end for make_shorts.py.

Source URL and the narration JSON are entered here; anything left blank falls
back to a saved default, so a run never needs the terminal.
"""

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).parent.resolve()
PY = ROOT / ".venv" / "bin" / "python"
SCRIPT = ROOT / "make_shorts.py"
WORK = ROOT / "work"
OUTPUT = ROOT / "output"
CHECK_DIR = ROOT / "work" / "check"
DEFAULTS_FILE = ROOT / "ui_defaults.json"
TEMPLATE = ROOT / "texts.template.json"

STYLES = ["bold", "yellow", "neon", "boxed", "small", "minimal"]
VOICES = [
    "en-US-AvaNeural", "en-US-AndrewNeural", "en-US-EmmaNeural",
    "en-US-BrianNeural", "en-US-JennyNeural", "en-GB-SoniaNeural",
    "en-GB-RyanNeural", "en-AU-NatashaNeural", "en-IN-NeerjaNeural",
    "en-IN-PrabhatNeural",
]


def load_defaults():
    if DEFAULTS_FILE.exists():
        try:
            return json.loads(DEFAULTS_FILE.read_text())
        except Exception:                                        # noqa: BLE001
            pass
    return {"url": "", "texts": ""}


def save_defaults(d):
    DEFAULTS_FILE.write_text(json.dumps(d, indent=2, ensure_ascii=False))


st.set_page_config(page_title="Shorts Maker", page_icon="🎬", layout="wide")
st.title("🎬 Shorts Maker")
st.caption("Long video → fixed-length vertical shorts with TTS voiceover and "
           "word-timed captions. Fully local: yt-dlp + edge-tts + ffmpeg.")

defaults = load_defaults()
WORK.mkdir(exist_ok=True)
OUTPUT.mkdir(exist_ok=True)

# ----------------------------------------------------------------- source
st.header("1 · Source video")
_SRC_MODES = ["Reuse last download", "YouTube / URL", "Upload a file"]
_have_cached = (WORK / "source.mp4").exists()
mode = st.radio("Where does the footage come from?", _SRC_MODES,
                index=0 if _have_cached else 1, horizontal=True)

url = local_video = None
cookies_browser = defaults.get("cookies_browser", "chrome")
if mode == "YouTube / URL":
    url = st.text_input("Video URL", value=defaults.get("url", ""),
                        placeholder="https://www.youtube.com/watch?v=...")
    if not url:
        st.info("Leave blank to fall back to the saved default URL, if there is one.")
    cookies_browser = st.selectbox(
        "Browser cookies", ["chrome", "safari", "firefox", "brave", "edge", "none"],
        index=["chrome", "safari", "firefox", "brave", "edge", "none"].index(
            defaults.get("cookies_browser", "chrome")),
        help="YouTube blocks unauthenticated downloads with 'Sign in to confirm "
             "you are not a bot'. Cookies from a signed-in browser fix it. "
             "Chrome works on this machine; Safari needs Full Disk Access.")
    if cookies_browser == "safari":
        st.warning("Safari's cookie file is protected by macOS. Grant your terminal "
                   "Full Disk Access in System Settings > Privacy & Security, "
                   "or use Chrome.")
elif mode == "Upload a file":
    up = st.file_uploader("Gameplay / source video", type=["mp4", "mov", "mkv", "webm"])
    if up:
        local_video = WORK / f"upload_{up.name}"
        local_video.write_bytes(up.getbuffer())
        st.success(f"Saved {up.name} ({local_video.stat().st_size / 1e6:.0f} MB)")
else:
    cand = WORK / "source.mp4"
    if cand.exists():
        local_video = cand
        st.success(f"Reusing {cand.name} ({cand.stat().st_size / 1e6:.0f} MB)")
    else:
        st.warning("No previous download found in work/. Pick another option.")

# ------------------------------------------------------------------ texts
st.header("2 · Narration text")
tmode = st.radio("How do you want to supply the 50 lines?",
                 ["Paste JSON", "Upload JSON", "Use default template"],
                 horizontal=True)

texts_json = None
n_texts = None
if tmode == "Paste JSON":
    txt = st.text_area(
        'JSON: {"1": "first line", "2": "second line", ...}',
        value=defaults.get("texts", ""), height=220,
        placeholder='{\n  "1": "Your first narration line.",\n'
                    '  "2": "Your second narration line."\n}')
    if txt.strip():
        try:
            parsed = json.loads(txt)
            texts_json = txt
            n_texts = len(parsed)
            st.success(f"Valid JSON · {n_texts} lines")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
elif tmode == "Upload JSON":
    up = st.file_uploader("texts.json", type=["json"])
    if up:
        texts_json = up.getvalue().decode("utf-8")
        try:
            n_texts = len(json.loads(texts_json))
            st.success(f"Valid JSON · {n_texts} lines")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")
            texts_json = None
else:
    texts_json = TEMPLATE.read_text() if TEMPLATE.exists() else None
    if texts_json:
        try:
            n_texts = len(json.loads(texts_json))
        except json.JSONDecodeError:
            n_texts = None
    st.info(f"Using texts.template.json — {n_texts or '?'} placeholder lines.")

# --------------------------------------------------------------- settings
st.header("3 · Settings")
c1, c2, c3, c4 = st.columns(4)
# Always a manual field: it starts empty and whatever you type stays put, no
# matter what the JSON contains. Empty simply means "all of them".
_max_count = max(500, n_texts or 0)
count = c1.number_input(
    "How many shorts", min_value=1, max_value=_max_count, value=None, step=1,
    placeholder=(f"empty = all {n_texts}" if n_texts else "e.g. 50"),
    help="Type how many shorts to render. Leave it empty to use every line "
         "in your JSON.")
# What the buttons and the clip picker actually act on.
n_clips = int(count) if count else (n_texts or 0)
duration = c2.number_input("Seconds each", 5.0, 180.0, 30.0, step=1.0)
start = c3.number_input("Skip intro (s)", 0.0, 100000.0, 0.0, step=5.0)
jobs = c4.number_input("Parallel encodes", 1, 12, 4)

c1, c2, c3 = st.columns(3)
voice = c1.selectbox("Voice", VOICES)
duck = c2.slider("Source audio under voiceover", 0.0, 1.0, 0.12, 0.01,
                 help="0 mutes the original audio entirely")
encoder = c3.selectbox("Encoder", ["videotoolbox", "libx264"],
                       help="videotoolbox uses the M-series hardware encoder")

# --------------------------------------------------------------- captions
st.header("4 · Captions")
smode = st.radio("Style assignment", ["One style for all", "Per-block plan"],
                 horizontal=True)
style = plan = None
if smode == "One style for all":
    style = st.selectbox("Style", STYLES)
else:
    plan = st.text_input("Plan", value="bold:10,yellow:10,boxed:10,neon:10,minimal:10",
                         help="name:count, consecutive clips, comma separated")

c1, c2, c3, c4 = st.columns(4)
position = c1.selectbox("Placement", ["center", "top", "bottom", "exact %"])
pos_pct = c2.number_input("Exact %", 0.0, 100.0, 50.0, step=1.0,
                          disabled=position != "exact %")
words = c3.number_input("Words per card", 0, 12, 0,
                        help="0 keeps the style's own value")
fsize = c4.number_input("Font size", 0, 200, 0, help="0 keeps the style's own value")
upper = st.checkbox("Force UPPERCASE off (--no-uppercase)")

# -------------------------------------------------------------------- run
st.header("5 · Generate")


def build_cmd(only=None, outdir=None):
    """Same arguments for the single check-video and the full batch, so what you
    approve is exactly what the batch produces."""
    cmd = [str(PY), str(SCRIPT), "--texts", str(WORK / "ui_texts.json"),
           "--duration", str(float(duration)),
           "--start", str(float(start)), "--jobs", str(int(jobs)),
           "--voice", voice, "--duck", str(float(duck)), "--encoder", encoder]
    # Omitted entirely when the field is blank, so make_shorts uses every text.
    if count:
        cmd += ["--count", str(int(count))]
    if mode == "YouTube / URL":
        cmd += ["--url", url or defaults["url"]]
        if cookies_browser != "none":
            cmd += ["--cookies-from-browser", cookies_browser]
    else:
        cmd += ["--video", str(local_video)]
    cmd += ["--style-plan", plan] if plan else ["--style", style]
    if position == "exact %":
        cmd += ["--position-percent", str(float(pos_pct))]
    else:
        cmd += ["--position", position]
    if words:
        cmd += ["--words-per-caption", str(int(words))]
    if fsize:
        cmd += ["--font-size", str(int(fsize))]
    if upper:
        cmd += ["--no-uppercase"]
    if only:
        cmd += ["--only", str(int(only))]
    if outdir:
        cmd += ["--outdir", str(outdir)]
    return cmd


def inputs_ok():
    if mode == "YouTube / URL" and not (url or defaults.get("url")):
        st.error("Enter a URL, or pick another source option.")
        return False
    if mode != "YouTube / URL" and not local_video:
        st.error("No source video selected.")
        return False
    if not texts_json:
        st.error("No narration text supplied.")
        return False
    return True


def execute(cmd, spinner):
    save_defaults({"url": url or defaults.get("url", ""),
                   "cookies_browser": cookies_browser,
                   "texts": texts_json if tmode == "Paste JSON"
                   else defaults.get("texts", "")})
    (WORK / "ui_texts.json").write_text(texts_json, encoding="utf-8")
    st.code(" ".join(cmd), language="bash")
    log_box = st.empty()
    lines = []
    with st.spinner(spinner):
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                log_box.code("\n".join(lines[-25:]))
        proc.wait()
    return proc.returncode


st.markdown("**Make one video first**, check it, then run the batch. Both use "
            "the settings above, so the batch matches what you approve.")
c1, c2 = st.columns([1, 2])
check_n = c1.number_input("Which clip", 1, max(1, n_clips), 1,
                          help="Rendered at its real position in the source "
                               "with its real style, exactly as in a full batch")
if c2.button(f"①  Generate clip {int(check_n)} only", use_container_width=True):
    if inputs_ok():
        rc = execute(build_cmd(only=int(check_n), outdir=CHECK_DIR),
                     f"Rendering clip {int(check_n)}…")
        st.session_state["check_rc"] = rc

made = sorted(CHECK_DIR.glob("*.mp4")) if CHECK_DIR.exists() else []
if made:
    st.subheader("Check this before running the batch")
    cc1, cc2 = st.columns([1, 2])
    with cc1:
        st.video(str(made[-1]))
    with cc2:
        st.caption(made[-1].name)
        st.caption("Happy with the size, placement and pacing? Run the batch "
                   "below. Otherwise change the settings above and generate "
                   "this one again.")

st.divider()
_all_label = f"all {n_clips}" if n_clips else "all"
if st.button(f"②  Generate {_all_label} videos", type="primary",
             use_container_width=True):
    if inputs_ok():
        rc = execute(build_cmd(), "Running — synthesizing, then encoding…")
        (st.success if rc == 0 else st.error)(f"Finished with exit code {rc}")

# ---------------------------------------------------------------- results
vids = sorted(OUTPUT.glob("*.mp4"))
if vids:
    st.header(f"Output · {len(vids)} videos")
    st.caption(f"Saved in {OUTPUT}")
    for row in range(0, min(len(vids), 12), 4):
        for col, v in zip(st.columns(4), vids[row:row + 4]):
            with col:
                st.video(str(v))
                st.download_button("Download", v.read_bytes(), file_name=v.name,
                                   key=f"dl_{v.name}", use_container_width=True)
    if len(vids) > 12:
        st.caption(f"…and {len(vids) - 12} more in the output folder.")
