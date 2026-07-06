# Podcast API

Web API that converts a **URL or PDF** into audio (MP3), in one of three modes:

| mode | what you get | speakers |
|---|---|---|
| `podcast` (default) | 5-8 min two-host conversational episode | HOST_A + HOST_B |
| `summary` (alias: `resume`) | 2-4 min spoken digest of the source | one narrator |
| `readout` | the full document adapted for listening | one narrator |

Pipeline: extract text → Claude (Opus 4.8) writes the script → edge-tts
synthesizes each speaker with a neural voice → one MP3.

## Setup

```sh
uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python
export ANTHROPIC_API_KEY=sk-ant-...   # required for script generation
```

(Instead of an API key you can run `ant auth login` — the SDK picks up the profile.)

## Run

```sh
.venv/bin/uvicorn app.main:app --port 8000
```

**Workbench UI: http://localhost:8000/** — a full studio for creating and
managing generations: library of previous productions (persists across
restarts), the script → slides → voice → outputs pipeline with per-stage
iteration, a reusable template library, inline players, and downloads.

Interactive API docs: http://localhost:8000/docs

## Usage

Start a job (`url` form field or PDF `file` upload — exactly one):

```sh
# Default: two-host podcast
curl -X POST localhost:8000/podcasts -F "url=https://example.com/article"

# From a PDF
curl -X POST localhost:8000/podcasts -F "file=@paper.pdf"

# Spoken summary with a custom reader personality
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" \
  -F "mode=summary" \
  -F "reader=a dry-witted British professor who can't resist a sardonic aside"

# Tailor any mode to a specific audience
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" \
  -F "mode=summary" \
  -F "audience=startup founders dealing with stress and the fear of failure"

# Any mode + a slide deck synced to the audio (web slideshow and/or MP4 video)
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=web"     # -> /podcasts/<id>/slides
curl -X POST localhost:8000/podcasts \
  -F "file=@paper.pdf" -F "slides=video"                   # -> /podcasts/<id>/video (+ /slides)

# Captions: burned into the output, or toggleable while playing
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=video" -F "captions=burned"
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=web" -F "captions=toggle"

# Style the slides after your own templates (PowerPoint, CSS, style guide, screenshots)
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=video" \
  -F "templates=@corporate_theme.pptx" \
  -F "templates=@brand_guide.md"

# Set expectations for the deck itself
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=web" \
  -F "slide_style=ultra minimal: one short phrase per slide, like a keynote"
curl -X POST localhost:8000/podcasts \
  -F "file=@paper.pdf" -F "slides=video" \
  -F "slide_style=much more informative than the voice-over: reference slides with data, quotes and examples from the source that the narration skips"

# Full readout with a specific voice
curl -X POST localhost:8000/podcasts \
  -F "file=@paper.pdf" -F "mode=readout" -F "voice=en-GB-RyanNeural"

# Podcast with custom host personalities and voices
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" \
  -F "host_a=an over-caffeinated startup founder who relates everything to product-market fit" \
  -F "host_b=a skeptical historian who keeps the founder honest" \
  -F "voice_a=en-US-GuyNeural" -F "voice_b=en-GB-SoniaNeural"
```

### Options (all optional form fields)

| field | applies to | meaning |
|---|---|---|
| `mode` | all | `podcast` \| `summary` (alias `resume`) \| `readout` |
| `host_a`, `host_b` | podcast | free-text personality of each host |
| `reader` | summary, readout | free-text personality of the narrator |
| `audience` | all | who's listening and what they care about — shifts emphasis, examples, and level of explanation |
| `voice_a`, `voice_b` | podcast | edge-tts voice per host |
| `voice` | summary, readout | edge-tts voice of the narrator |
| `slides` | all | `web` — synced HTML slideshow; `video` — MP4 with voice-over (also includes the web version). Requires ffmpeg for video. |
| `slide_style` | with `slides` | expectations for the deck — e.g. `minimal, one phrase per slide` or `much more informative than the voice-over, with data and quotes from the source` |
| `captions` | with `slides` | on-screen text of the narration: `burned` (baked in at generation, always visible) or `toggle` (choose while playing — CC button in the web player, subtitle track in the video) |
| `templates` | with `slides` | zero or more template/style-guide files (repeat the field): `.pptx`/`.potx` (theme colors + fonts extracted), HTML/CSS, images/screenshots of slides you like, `.pdf`, `.md`/`.txt` style guides. A visual theme (colors, fonts) and content rules are derived and applied to the web slideshow and video. |

Slides are not text-only: images found in the source (article images,
PDF-embedded figures) are extracted into the job's `assets/` and placed on
slides where they fit, and the model designs its own **figures** — charts of
any form, timelines, diagrams — as themed SVG (crisp in the web slideshow,
rasterized into the video). Figure data is taken from the source, never
invented.

List available voices: `.venv/bin/edge-tts --list-voices` (defaults:
`en-US-GuyNeural`, `en-US-AriaNeural`).

Poll until done (steps: `extracting` → `writing_script` → `synthesizing_audio`):

```sh
curl localhost:8000/podcasts/<job_id>
```

Download the results:

```sh
curl -OJ localhost:8000/podcasts/<job_id>/audio        # MP3
curl -OJ localhost:8000/podcasts/<job_id>/transcript   # timestamped transcript (.txt, every job)
open  http://localhost:8000/podcasts/<job_id>/slides   # synced web slideshow
curl -OJ localhost:8000/podcasts/<job_id>/video        # MP4 (slides=video only)
```

The finished status response also includes the full script (speaker + text per
line). The web slideshow is a single self-contained HTML file (audio embedded)
that advances slides in sync with playback — arrow keys jump between slides,
space toggles play, CC toggles captions (when generated with `captions=`).

## Iterative workflow

Instead of one-shot generation, you can iterate on each layer before paying
for the next:

```sh
# 1. Stop after the transcript draft
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "mode=summary" -F "until=script"

# 2. Iterate on the script (repeat as often as needed)
curl -X POST localhost:8000/podcasts/<id>/revise \
  -F "target=script" \
  -F "instructions=Open with a provocative question. Cut the third paragraph."

# 3. Iterate on the slides (creates the deck on first call)
curl -X POST localhost:8000/podcasts/<id>/revise \
  -F "target=slides" \
  -F "instructions=Make every slide title a question, max 3 bullets."

# 4. Produce voice-over + slides + captions
curl -X POST localhost:8000/podcasts/<id>/render -F "slides=web" -F "captions=toggle"

# 5. Iterate on the voice-over, referencing transcript line numbers
curl -X POST localhost:8000/podcasts/<id>/revise \
  -F "target=voice" \
  -F "instructions=Line 0: slow down 12% with a pause after the question. \
Everywhere: pronounce 'Graham' as 'gram'."
```

Notes on the loop:

- `until` on create: `script` (stop after transcript draft), `slides` (stop
  after deck), `full` (default — everything).
- `GET /podcasts/<id>` always shows the current numbered `script`, the
  `slides_deck`, any `voice_delivery` direction, `stale` flags, and the
  revision history — review each iteration there before the next one.
- `target=voice` revisions change *delivery only*: per-line phonetic
  respelling of the spoken text, rate (`-12%`), pitch, volume, or voice. The
  displayed transcript and captions keep the original wording. Audio is
  re-synthesized automatically so you can listen right away.
- `target=script` revisions reset voice direction (line numbers change) and
  mark slides/audio stale; the next `/render` brings everything back in sync.
- Slide revisions re-render the slideshow/video immediately when audio
  already exists.

## Notes

- Every generation lives in its own folder `data/<job_id>/` with everything
  related to it: `audio.mp3`, `transcript.txt`, `captions.srt`, `slides.html`,
  `video.mp4`, the current `script.json` / `deck.json` / `theme.json` /
  `meta.json` state, and the uploaded `templates/`.
- The library persists: on startup the server rehydrates every generation
  from its `data/<job_id>/` folder, so previous work stays browsable and
  revisable across restarts. `GET /podcasts` lists them; `DELETE
  /podcasts/<id>` removes one. Reusable template sets live under
  `data/_templates/` via `GET/POST/DELETE /templates` (reference them at
  create time with `template_id`).
- Scripts are written in English regardless of source language.
- Sources over 400k characters are rejected — split large documents. Very long
  documents may exceed the readout script budget; use `mode=summary` instead.
- Scanned PDFs without a text layer and JavaScript-only pages can't be extracted.
