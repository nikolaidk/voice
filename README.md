# Fluent Agents Studio

**[fluentagents.com](https://fluentagents.com)** — an agentic workflow, humanised: a multi-agent
production pipeline (extract → script → deck → voice → render) dressed up
for human interaction, converting a **URL or PDF** into produced audio and
video content. Built as an AI-assisted development experiment directed by
Nikolai Manigoff Rasmussen, with Claude (Fable 5) as the implementer —
[the experiment write-up on LinkedIn](https://www.linkedin.com/posts/nikolai-rasmussen-8420a818_aiassisteddevelopment-softwarearchitecture-ugcPost-7479980087472463873-0063/),
[the live demo](https://demo.fluentagents.com/studio).

## The experiment

This repository is the artifact of a deliberate **under-specification
experiment**: an empty directory, one prompt — *"Create a web API that takes a
URL or PDF and converts it into a podcast"* — and only intent, rationale and
occasional course corrections from the human, while stock **Claude Code
(Fable 5), with no extra add-ons**, made the engineering decisions — Fable 5
being the most capable model publicly available at the time: the finding below
was produced at the frontier, not with a budget model. Fewer than
500 human words of direction became ~3,400 lines of working code, 17 endpoints
and a five-stage media pipeline in an evening; the code review that redirected
the architecture was four words long ("you hardcoded the charts"). The result
is **experimental, not production-ready** — not security-hardened, not
load-tested, and deliberately left that way as part of the record.

**The conclusion cuts the other way: humans are needed more than ever.** Even
the frontier model is no holy grail: not once did it propose a direction or
say "this is enough"; it wrote
thousands of lines and zero tests; twice it reported success on
changes that silently did nothing; and it missed flaws any user catches at a
glance (duplicate library entries, mixed-language UI strings, a tutorial video
where every screenshot showed the same screen). Everything it lacked — taste,
dissatisfaction, verification, standards — had to come from the human side of
the prompt. The human contribution is not disappearing; it is concentrating:
capability can be rented, judgment cannot.

- Live demo: **[fluentagents.com](https://fluentagents.com)** (read-only demo mode)
- Experiment write-up: **[LinkedIn post](https://www.linkedin.com/posts/nikolai-rasmussen-8420a818_aiassisteddevelopment-softwarearchitecture-ugcPost-7479980087472463873-0063/)**
- Author: **[Nikolai Manigoff Rasmussen](https://www.linkedin.com/in/nikolai-rasmussen-8420a818/)**

Three modes:

| mode | what you get | speakers |
|---|---|---|
| `podcast` (default) | 5-8 min two-host conversational episode | HOST_A + HOST_B |
| `summary` (alias: `resume`) | 2-4 min spoken digest of the source | one narrator |
| `readout` | the full document adapted for listening | one narrator |

Pipeline (five stages): **extract** text + images from the source →
**script** written by Claude (Opus 4.8), shaped by mode, personalities, and
audience → **deck** designed against the script (source images placed,
figures designed by the model as themed SVG) → **voice** synthesized per
line with edge-tts, honoring per-line delivery direction → **render** into
MP3, timestamped transcript, captions, a self-contained web slideshow, and
MP4 video.

## Setup

Prerequisites (macOS):

```sh
brew install ffmpeg cairo   # ffmpeg: video rendering · cairo: SVG figure rasterization
```

Then:

```sh
uv venv .venv
uv pip install -r requirements.txt --python .venv/bin/python
export ANTHROPIC_API_KEY=sk-ant-...   # required for script/deck generation
```

(Instead of an API key you can run `ant auth login` — the SDK picks up the
profile.) Without ffmpeg, everything except `slides=video` works.

## Run

```sh
.venv/bin/uvicorn app.main:app --port 8000
```

**Landing page: http://localhost:8000/** — the public front door: the guide
video as a hero demo, a feature tour, and links into every production as a
synced slideshow. In demo mode this is what visitors see first.

**Workbench UI (Fluent Agents Studio): http://localhost:8000/studio** — a full studio for creating and
managing generations: library of previous productions (persists across
restarts), the script → slides → voice → outputs pipeline with per-stage
iteration, an asset inventory showing which image or figure lands on which
slide, a reusable template library, inline players, and downloads.

**About page: http://localhost:8000/about** — the story behind the
experiment, with the author card and LinkedIn/GitHub links.

Interactive API docs: http://localhost:8000/docs

Landing and about pages are responsive; the workbench is a desktop tool and
shows a minimum-requirements gate (with links to the landing page and guide)
on screens smaller than 920×520.

## User guide

**[docs/user-guide.html](docs/user-guide.html)** — an 11-minute video-style
walkthrough of the entire workbench, from first generation to finished
production, made for complete beginners. It is a self-contained slideshow
(voice-over embedded, synced slides, toggleable captions — open it in any
browser) and was produced by the workbench itself from a written guide, so it
doubles as a demo of the output quality. Also available:
[the transcript](docs/user-guide-transcript.txt) and
[a PowerPoint with the narration as speaker notes](docs/user-guide.pptx).
The MP4 version lives in the workbench library ("Fluent Agents Studio — the
complete user guide").

## Demo mode

Deploy a read-only showcase with the bundled example productions — no API
key, no ffmpeg, no LLM calls:

```sh
FLUENT_DEMO=1 .venv/bin/uvicorn app.main:app --port 8000
```

The workbench serves the projects in `demo/data/` (a snapshot of real
productions, ~55 MB, committed with the repo): visitors can browse the
library, read scripts and decks, play audio, watch the slideshows and
videos, and download everything. Every editing control stays **visible but
locked** — activating one shows a toast explaining what it would do in the
full studio ("agentic pain relief" as a guided tour) — and the server
rejects all mutations with 403 regardless. A "DEMO · READ-ONLY" badge shows
in the sidebar. To refresh the demo content, copy `data/` to `demo/data/`
and commit.

### Deploy the demo to Azure

```sh
./scripts/deploy_azure_demo.sh          # defaults: resource group `voice`, app `fluentagents-demo`
APP=my-name SKU=F1 ./scripts/deploy_azure_demo.sh   # override names/SKU
```

The script is idempotent — run it again for every redeploy. It creates a
Linux App Service plan and web app if missing, sets `FLUENT_DEMO=1` and the
uvicorn startup command, zips `app/` + `demo/` + `requirements.txt`, deploys
with a remote build, and verifies the live `/config` endpoint reports demo
mode before declaring success.

The public deployment spans two domains on the same web app, both with
free auto-renewing App Service managed certificates and HTTPS enforced:

| URL | serves |
|---|---|
| **https://fluentagents.com** | landing page + about (GoDaddy `A @ -> app inbound IP`, `TXT asuid`) |
| **https://demo.fluentagents.com** | the read-only demo studio (`CNAME demo -> fluentagents-demo.azurewebsites.net`, `TXT asuid.demo`) |

The studio's logo links back to the apex landing when running on the demo
subdomain. Redeploys via the script don't touch the domain setup.

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

# Any mode + a slide deck synced to the audio (web slideshow, MP4 video, PowerPoint)
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=web"     # -> /podcasts/<id>/slides
curl -X POST localhost:8000/podcasts \
  -F "file=@paper.pdf" -F "slides=video,pptx"              # -> /video + /pptx (+ /slides)

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

# ...or reference a saved template set from the template library
curl -X POST localhost:8000/templates -F "name=Corporate" -F "files=@theme.pptx"
curl -X POST localhost:8000/podcasts \
  -F "url=https://example.com/article" -F "slides=web" -F "template_id=<id>"

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
| `slides` | all | comma-separated output formats: `web` (synced HTML slideshow), `video` (MP4 with voice-over; needs ffmpeg), `pptx` (PowerPoint with the narration as speaker notes per slide) — e.g. `slides=video,pptx`. Any format includes the web slideshow. |
| `slide_style` | with `slides` | expectations for the deck — e.g. `minimal, one phrase per slide` or `much more informative than the voice-over, with data and quotes from the source` |
| `captions` | with `slides` | on-screen text of the narration: `burned` (baked in at generation, always visible) or `toggle` (choose while playing — CC button in the web player, subtitle track in the video) |
| `animations` | with `slides` | `on` animates the production: staggered entrance of titles/bullets/visuals in the web slideshow, bullet-by-bullet build-in in the video, and the model may animate its SVG figures (bars growing, lines drawing). PPTX stays static. |
| `template_id` | with `slides` | reference a saved template set from the template library instead of (or alongside) uploading files |
| `footer` | with `slides` | traceability stamp rendered on every slide across web, video and PowerPoint — e.g. `footer=https://fluentagents.com` |
| `templates` | with `slides` | zero or more template/style-guide files (repeat the field): `.pptx`/`.potx` (theme colors + fonts extracted), HTML/CSS, images/screenshots of slides you like, `.pdf`, `.md`/`.txt` style guides. A visual theme (colors, fonts) and content rules are derived and applied to the web slideshow and video. |

Slides are not text-only: images found in the source (article images,
PDF-embedded figures) are extracted into the job's `assets/` and placed on
slides where they fit, and the model designs its own **figures** — charts of
any form, timelines, diagrams — as themed SVG (crisp in the web slideshow,
rasterized into the video). Figure data is taken from the source, never
invented.

List available voices: `.venv/bin/edge-tts --list-voices` (defaults:
`en-US-GuyNeural`, `en-US-AriaNeural`).

Poll until done (steps: `extracting` → `reading_templates` →
`writing_script` → `designing_slides` → `synthesizing_audio` →
`rendering_slides` → `rendering_pptx` → `rendering_video`, skipping the ones
a job doesn't need):

```sh
curl localhost:8000/podcasts/<job_id>
```

Download the results:

```sh
curl -OJ localhost:8000/podcasts/<job_id>/audio        # MP3
curl -OJ localhost:8000/podcasts/<job_id>/transcript   # timestamped transcript (.txt, every job)
open  http://localhost:8000/podcasts/<job_id>/slides   # synced web slideshow
curl -OJ localhost:8000/podcasts/<job_id>/video        # MP4 (slides=video)
curl -OJ localhost:8000/podcasts/<job_id>/pptx         # PowerPoint (slides=pptx)
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
- **Source replacement**: `POST /podcasts/<id>/source` (or the "Replace
  source" button on the job page) re-extracts a new URL or replacement PDF
  into the *same* project — identity, options, templates, revision history
  and spend all survive; text and images are replaced, the deck is marked
  stale, and a script revision brings the words in line with the new source.
- **Timing**: nudge when slides appear relative to the narration without
  touching the audio — `POST /podcasts/<id>/timing -F 'offsets={"4": -2,
  "7": 1.5}'` (0-based slide index → seconds, negative = earlier; `0`
  clears). Outputs re-render with the new times. In the workbench, every
  slide card shows its start time with an editable offset. Offsets reset
  when the deck is revised (slide indices shift).
- **Per-slide voice review**: in the workbench Slides tab, the ▶ on any
  slide card opens a review overlay — the slide rendered large, playing
  exactly that slide's narration segment, with prev/next, replay, and
  auto-advance. Review a slide, nudge its offset, apply, listen again.

## Notes

- Every generation lives in its own folder `data/<job_id>/` with everything
  related to it: `audio.mp3`, `transcript.txt`, `captions.srt`, `slides.html`,
  `video.mp4`, the source text (`source.txt`), the current `script.json` /
  `deck.json` / `theme.json` / `meta.json` state, the uploaded `templates/`,
  and `assets/` (images extracted from the source plus the model-designed
  figures as `.svg` + rasterized `.png`, served at
  `/podcasts/<id>/assets/<name>`).
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
- **Publish to YouTube**: `POST /podcasts/<id>/publish/youtube` (or the
  YouTube card on the Outputs page) uploads the production's video to your
  channel — **private by default**; review it on YouTube before flipping to
  unlisted/public. One-time setup: create a Google Cloud project with the
  YouTube Data API v3 enabled, create an OAuth "Desktop app" client, save
  its JSON as `data/_youtube/client_secret.json`, then run
  `.venv/bin/python scripts/youtube_auth.py` (browser consent; stores a
  refresh token). Note Google's default quota allows ~6 uploads/day, and
  unverified apps only accept accounts added as test users.
- **Cost control**: every job tracks its Claude usage — `usage` in the status
  response (and a spend chip in the workbench) shows tokens, calls, and an
  estimated cost in USD. Revisions use prompt caching: the large stable
  context (source text, script, image assets) is cached across iterations of
  the same job, so a second and third revision within a few minutes bill the
  repeated prefix at ~10% of the input price. Iterate in bursts to stay
  inside the 5-minute cache window.

## License

[MIT](LICENSE) © 2026 Nikolai Manigoff Rasmussen
