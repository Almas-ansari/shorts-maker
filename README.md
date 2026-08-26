# shorts-maker

Turn one long video into a batch of vertical shorts, each with its own
voiceover and word-timed captions burned in.

Point it at a YouTube URL or a local file, give it a JSON file of narration
lines, and it produces one 9:16 video per line — gameplay or B-roll underneath,
synthesized speech on top, captions timed to the actual words.

**Everything runs locally. No API keys, no accounts, no paid services, no LLM.**
Speech comes from Microsoft Edge's free TTS endpoint, captions are timed from
that audio's own word boundaries, and ffmpeg ships with the Python dependency.

## Requirements

- **Python 3.11+**
- **macOS or Linux** (`webui.sh` / `run_shorts.sh` are POSIX shell; the Python
  works anywhere)
- **A JavaScript runtime — only for YouTube downloads.** yt-dlp needs one for
  YouTube extraction:
  ```sh
  brew install deno        # macOS
  ```
  Not needed if you only ever pass local files with `--video`.

ffmpeg does **not** need to be installed — `imageio-ffmpeg` provides a static
binary, and the code points yt-dlp at it too.

## Setup

```sh
git clone https://github.com/Almas-ansari/shorts-maker
cd shorts-maker
./setup.sh
```

`setup.sh` uses [uv](https://docs.astral.sh/uv/) when available and falls back
to `python3 -m venv` + pip.

## Use it

### Web UI

```sh
./webui.sh              # http://127.0.0.1:8502
```

Source, narration JSON, clip length, voice, caption style and placement are all
on the page. It runs on port 8502 to stay out of the way of other local apps.

The UI is deliberately two-step: **generate one video first**, look at it, then
run the whole batch. Both buttons build the same command, so the batch is
guaranteed to match the single video you approved.

### CLI

```sh
# from a local file
./run_shorts.sh --video gameplay.mp4 --texts texts.json

# from YouTube (needs cookies, see troubleshooting)
./run_shorts.sh --url "https://youtube.com/watch?v=..." --texts texts.json \
                --cookies-from-browser chrome

# reuses the cached download when no source is given
./run_shorts.sh --texts texts.json

# render only clip 7, to check settings before committing to a batch
./run_shorts.sh --texts texts.json --only 7 --outdir work/check
```

## Input format

A JSON object keyed by clip number, or a plain array:

```json
{
  "1": "First clip's narration.",
  "2": "Second clip's narration."
}
```

Copy `texts.template.json` to `texts.json` and fill it in. `texts.json` is
gitignored, so your content never lands in the repo.

**Length matters.** Aim for roughly 2.7 words per second of clip: about
75 words for a 30s short. Overlong text is sped up to a maximum of 1.30×, and
anything still too long is reported by clip number rather than silently cut.

## Caption styles

| Style | Look |
|---|---|
| `bold` | 88px Arial Black, white, heavy outline, UPPERCASE, 3 words/card |
| `yellow` | same but yellow — reads well on dark footage |
| `neon` | bright green, UPPERCASE |
| `boxed` | white on a translucent black box |
| `small` | 54px, normal case, 6 words/card |
| `minimal` | 52px, lower third, 7 words/card |

`./run_shorts.sh --list-styles` prints them with current values.

One style for the whole run:

```sh
--style bold
```

Or different styles per block of clips:

```sh
--style-plan "bold:10,yellow:10,boxed:10,neon:10,minimal:10"
```

Override any preset's values:

```sh
--font-size 132 --words-per-caption 7      # bigger text, more of it per card
--position top|center|bottom                # default: center
--position-percent 40                       # exact height, 0=top 100=bottom
--no-uppercase
```

## Options that matter

| Flag | Default | Notes |
|---|---|---|
| `--count N` | all texts in the JSON | render only the first N |
| `--duration S` | 30 | seconds per short, fixed |
| `--start S` | 0 | skip an intro before the first clip |
| `--only N` | — | render just clip N, at its real position and style |
| `--voice NAME` | `en-US-AvaNeural` | any Edge TTS voice |
| `--duck X` | 0.12 | source audio level under the voiceover; 0 mutes it |
| `--jobs N` | 4 | parallel encodes |
| `--encoder` | `videotoolbox` | hardware on Apple Silicon; `libx264` elsewhere |

## Behaviour worth knowing

- **Clip positions are evenly spaced** through the source: clip *n* starts at
  `start + (n-1) × duration`. If the source is shorter than
  `count × duration`, positions wrap around and footage repeats — it warns when
  this happens.
- **`output/` is never cleared.** New runs add to it. Clear it between batches:
  `rm -f output/*.mp4`
- **Intermediates are deleted** after each run; only the finished mp4s remain.
- **Filenames** come from the first words of each line, prefixed with the clip
  number.

## Measured throughput

50 × 30s shorts, `--jobs 4`, videotoolbox on an M4 MacBook Air:
**≈4 minutes** end to end, including 50 TTS requests.

## Troubleshooting YouTube

**`Sign in to confirm you're not a bot` / `HTTP 429`**

YouTube blocks unauthenticated downloads. Pass cookies from a browser you're
signed into:

```sh
--cookies-from-browser chrome
```

On macOS Chrome works out of the box. **Safari does not** unless you grant your
terminal Full Disk Access in System Settings → Privacy & Security, because its
cookie database is protected. Warnings about "account cookies no longer valid"
are harmless — the download still succeeds.

**`No supported JavaScript runtime could be found`**

```sh
brew install deno
```

**Fallback that always works:** download the video yourself, then pass
`--video <file>` (or use the UI's upload option).

## Privacy

Nothing leaves your machine except the video download and the TTS requests to
Microsoft's Edge endpoint. There is no telemetry, no account, and no key to
configure. `texts.json`, `ui_defaults.json`, `work/` and `output/` are
gitignored so your content and settings stay local.
