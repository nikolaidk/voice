"""Web API that converts a URL or PDF into audio (MP3), with optional synced
slides (web/video), captions, transcripts — and an iterative workflow:

  1. create with until=script        -> iterate:  POST /podcasts/{id}/revise target=script
  2. (optionally) until=slides       -> iterate:  POST /podcasts/{id}/revise target=slides
  3. POST /podcasts/{id}/render      -> voice-over + slides + captions
  4. iterate on the voice-over:         POST /podcasts/{id}/revise target=voice
     (instructions reference transcript line numbers; per-line pronunciation,
     rate, pitch, volume, voice)

One-shot generation still works: create with the default until=full.
"""

import asyncio
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import extract, pptx_out, script_gen, slides as slides_mod, text_out, tts
from .extract import ExtractionError

app = FastAPI(
    title="Podcast API",
    description=(
        "Convert a URL or PDF into audio: a two-host podcast, a spoken "
        "summary, or a full readout — with optional synced slides, captions, "
        "transcripts, and iterative revision of script, slides, and voice-over."
    ),
)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

MODES = {"podcast", "summary", "readout"}
MODE_ALIASES = {"resume": "summary"}
CAPTION_ALIASES = {"generating": "burned", "generate": "burned", "burn": "burned",
                   "runtime": "toggle", "running": "toggle"}
STAGES = {"script", "slides", "full"}
REVISE_TARGETS = {"script", "slides", "voice"}

TPL_DIR = DATA_DIR / "_templates"

# In-memory job store; artifacts persist on disk under data/ and are
# rehydrated on startup so the library survives restarts.
jobs: dict[str, dict] = {}

STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.on_event("startup")
async def _rehydrate() -> None:
    if not DATA_DIR.exists():
        return
    for d in sorted(DATA_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name in jobs:
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        try:
            _load_job_from_disk(d)
        except Exception:
            continue  # a broken folder shouldn't take the library down


def _load_job_from_disk(job_dir: Path) -> None:
    meta = json.loads((job_dir / "meta.json").read_text())
    job_id = job_dir.name
    job = {
        "status": "done",
        "step": None,
        "error": None,
        "mode": meta.get("mode", "podcast"),
        "job_dir": str(job_dir),
        "template_names": meta.get("templates", []),
        "_template_digests": [],
        "theme_obj": None,
        "source": meta.get("source"),
        "source_text": None,
        "title": meta.get("title"),
        "created_at": meta.get("created_at") or job_dir.stat().st_mtime,
        "options": meta.get("options", {
            "host_a": None, "host_b": None, "reader": None, "audience": None,
            "voices": {}, "slides": None, "slide_style": None, "captions": None,
        }),
        "script_obj": None,
        "deck_obj": None,
        "delivery": {},
        "timing": {int(k): v for k, v in (meta.get("timing") or {}).items()},
        "cues": meta.get("cues"),
        "stale": meta.get("stale", {"slides": False, "audio": False, "outputs": False}),
        "revisions": meta.get("revisions", []),
        "audio_path": None,
        "transcript_path": None,
        "slides_path": None,
        "video_path": None,
    }
    job["assets"] = sorted(
        f.name for f in (job_dir / "assets").glob("img_*.jpg")
    ) if (job_dir / "assets").exists() else []
    source_file = job_dir / "source.txt"
    if source_file.exists():
        job["source_text"] = source_file.read_text(encoding="utf-8")
    script_file = job_dir / "script.json"
    if script_file.exists():
        data = json.loads(script_file.read_text())
        job["script_obj"] = script_gen.PodcastScript(
            title=data["title"],
            lines=[
                script_gen.ScriptLine(speaker=l["speaker"], text=l["text"])
                for l in data["lines"]
            ],
        )
        job["delivery"] = {int(k): v for k, v in data.get("voice_delivery", {}).items()}
    deck_file = job_dir / "deck.json"
    if deck_file.exists():
        job["deck_obj"] = slides_mod.SlideDeck(
            slides=[slides_mod.Slide(**s) for s in json.loads(deck_file.read_text())]
        )
    theme_file = job_dir / "theme.json"
    if theme_file.exists():
        job["theme_obj"] = slides_mod.SlideTheme(**json.loads(theme_file.read_text()))
    if isinstance(job["options"].get("slides"), str):
        job["options"]["slides"] = [job["options"]["slides"]]
    job["pptx_path"] = None
    for key, name in (("audio_path", "audio.mp3"), ("transcript_path", "transcript.txt"),
                      ("slides_path", "slides.html"), ("video_path", "video.mp4"),
                      ("pptx_path", "slides.pptx")):
        if (job_dir / name).exists():
            job[key] = str(job_dir / name)
    jobs[job_id] = job


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
async def workbench() -> FileResponse:
    return FileResponse(STATIC_DIR / "workbench.html", media_type="text/html")


# ------------------------------------------------------------------ create

@app.post("/podcasts", status_code=202)
async def create_podcast(
    url: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    mode: str = Form("podcast"),
    host_a: Optional[str] = Form(None, description="Personality of host A (podcast mode)"),
    host_b: Optional[str] = Form(None, description="Personality of host B (podcast mode)"),
    reader: Optional[str] = Form(None, description="Personality of the narrator (summary/readout)"),
    audience: Optional[str] = Form(None, description="Who's listening and what they care about (all modes)"),
    voice_a: Optional[str] = Form(None, description="edge-tts voice for host A"),
    voice_b: Optional[str] = Form(None, description="edge-tts voice for host B"),
    voice: Optional[str] = Form(None, description="edge-tts voice for the narrator"),
    slides: Optional[str] = Form(
        None, description="Add a synced slide deck: 'web' (HTML slideshow) or 'video' (MP4)"
    ),
    slide_style: Optional[str] = Form(
        None,
        description="Expectations for the deck, e.g. 'minimal, one idea per slide' "
        "or 'much more informative than the voice-over, include data from the source'",
    ),
    captions: Optional[str] = Form(
        None,
        description="On-screen text for slides/video: 'burned' (baked in at "
        "generation, always visible) or 'toggle' (switch on/off while playing)",
    ),
    until: str = Form(
        "full",
        description="How far to run before pausing for iteration: 'script' "
        "(stop after the transcript draft), 'slides' (stop after the deck), "
        "or 'full' (produce everything)",
    ),
    templates: Optional[list[UploadFile]] = File(
        None,
        description="Zero or more template/style-guide files for the slides "
        "(.pptx, .html/.css, images, .pdf, .md) — repeat the field per file",
    ),
    template_id: Optional[str] = Form(
        None, description="ID of a saved template set from the template library"
    ),
) -> dict:
    """Start a conversion job. Provide either `url` or a PDF `file` (multipart form)."""
    if (url is None) == (file is None):
        raise HTTPException(
            status_code=422,
            detail="Provide exactly one of: `url` (form field) or `file` (PDF upload).",
        )

    mode = MODE_ALIASES.get(mode.lower().strip(), mode.lower().strip())
    if mode not in MODES:
        raise HTTPException(
            status_code=422,
            detail="`mode` must be one of: podcast, summary (alias: resume), readout.",
        )

    until = until.lower().strip() or "full"
    if until not in STAGES:
        raise HTTPException(
            status_code=422, detail="`until` must be one of: script, slides, full."
        )

    slides = _validate_slides(slides)
    if until == "slides" and slides is None:
        slides = ["web"]
    captions = _validate_captions(captions, slides)

    pdf_bytes = None
    pdf_name = None
    if file is not None:
        if file.content_type not in (None, "application/pdf", "application/octet-stream"):
            raise HTTPException(status_code=422, detail="Only PDF uploads are supported.")
        pdf_bytes = await file.read()
        pdf_name = file.filename or "Uploaded PDF"

    voices = {}
    if voice_a:
        voices["HOST_A"] = voice_a
    if voice_b:
        voices["HOST_B"] = voice_b
    if voice:
        voices["NARRATOR"] = voice

    job_id = uuid.uuid4().hex[:12]
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    template_digests = []
    template_names = []
    if template_id:
        tpl_dir = TPL_DIR / template_id
        tpl_meta = tpl_dir / "meta.json"
        if not tpl_meta.exists():
            raise HTTPException(status_code=404, detail="Unknown template_id.")
        tpl_name = json.loads(tpl_meta.read_text()).get("name", template_id)
        for f in sorted(tpl_dir.iterdir()):
            if f.name == "meta.json" or not f.is_file():
                continue
            template_digests.append(slides_mod.digest_template(f.name, f.read_bytes()))
            template_names.append(f"[{tpl_name}] {f.name}")
    for tf in templates or []:
        name = Path(tf.filename or "template").name
        data = await tf.read()
        if not data:
            continue
        (job_dir / "templates").mkdir(exist_ok=True)
        (job_dir / "templates" / name).write_bytes(data)
        template_digests.append(slides_mod.digest_template(name, data))
        template_names.append(name)
    jobs[job_id] = {
        "status": "queued",
        "step": None,
        "error": None,
        "mode": mode,
        "job_dir": str(job_dir),
        "template_names": template_names,
        "_template_digests": template_digests,
        "theme_obj": None,
        "source": url or pdf_name,
        "source_text": None,
        "title": None,
        "created_at": time.time(),
        "options": {
            "host_a": host_a, "host_b": host_b, "reader": reader,
            "audience": audience, "voices": voices,
            "slides": slides, "slide_style": slide_style, "captions": captions,
        },
        "script_obj": None,
        "deck_obj": None,
        "assets": [],
        "delivery": {},        # line index -> {spoken, rate, pitch, volume, voice}
        "timing": {},          # slide index -> offset seconds (negative = earlier)
        "cues": None,
        "stale": {"slides": False, "audio": False, "outputs": False},
        "revisions": [],
        "audio_path": None,
        "transcript_path": None,
        "slides_path": None,
        "video_path": None,
        "pptx_path": None,
    }
    task = asyncio.create_task(_create_pipeline(job_id, until, url, pdf_bytes, pdf_name))
    jobs[job_id]["_task"] = task

    return {
        "job_id": job_id,
        "status": "queued",
        "mode": mode,
        "until": until,
        "status_url": f"/podcasts/{job_id}",
    }


def _validate_slides(slides: Optional[str]) -> Optional[list[str]]:
    """Parse `slides` into a list of formats: web, video, pptx (comma-separated)."""
    if slides is None:
        return None
    wanted = [v.strip() for v in slides.lower().split(",") if v.strip()]
    if not wanted:
        return None
    for v in wanted:
        if v not in ("web", "video", "pptx"):
            raise HTTPException(
                status_code=422,
                detail="`slides` must be a comma-separated list of: web, video, pptx.",
            )
    if "video" in wanted and not slides_mod.ffmpeg_available():
        raise HTTPException(
            status_code=422,
            detail="slides=video requires ffmpeg on the server (brew install ffmpeg); "
            "web and pptx work without it.",
        )
    if "web" not in wanted:
        wanted.append("web")  # the web slideshow is free alongside any format
    return wanted


def _validate_captions(captions: Optional[str], slides: Optional[list]) -> Optional[str]:
    if captions is not None:
        captions = captions.lower().strip() or None
        captions = CAPTION_ALIASES.get(captions, captions)
    if captions not in (None, "burned", "toggle"):
        raise HTTPException(
            status_code=422,
            detail="`captions` must be 'burned' (baked in at generation) or "
            "'toggle' (switchable at runtime).",
        )
    if captions and not slides:
        raise HTTPException(
            status_code=422,
            detail="`captions` requires a visual output — also pass slides=web "
            "or slides=video. (A transcript file is always generated.)",
        )
    return captions


# ----------------------------------------------------------------- library

@app.get("/podcasts")
async def list_podcasts() -> list[dict]:
    out = []
    for job_id, job in jobs.items():
        out.append({
            "job_id": job_id,
            "title": job["title"],
            "mode": job["mode"],
            "source": job["source"],
            "status": job["status"],
            "step": job["step"],
            "stage": _stage(job),
            "created_at": job.get("created_at"),
            "has_audio": bool(job["audio_path"]),
            "has_slides": bool(job["slides_path"]),
            "has_video": bool(job["video_path"]),
            "has_pptx": bool(job.get("pptx_path")),
            "templates": job["template_names"],
            "revision_count": len(job["revisions"]),
        })
    out.sort(key=lambda j: j["created_at"] or 0, reverse=True)
    return out


@app.delete("/podcasts/{job_id}")
async def delete_podcast(job_id: str) -> dict:
    job = _get_job(job_id)
    _require_idle(job)
    shutil.rmtree(job["job_dir"], ignore_errors=True)
    jobs.pop(job_id, None)
    return {"deleted": job_id}


# ------------------------------------------------------- template library

@app.get("/templates")
async def list_templates() -> list[dict]:
    out = []
    if TPL_DIR.exists():
        for d in sorted(TPL_DIR.iterdir()):
            meta = d / "meta.json"
            if not d.is_dir() or not meta.exists():
                continue
            info = json.loads(meta.read_text())
            out.append({
                "template_id": d.name,
                "name": info.get("name", d.name),
                "files": info.get("files", []),
                "created_at": info.get("created_at"),
            })
    out.sort(key=lambda t: t["created_at"] or 0, reverse=True)
    return out


@app.post("/templates", status_code=201)
async def create_template(
    name: str = Form(...),
    files: list[UploadFile] = File(...),
) -> dict:
    if not name.strip():
        raise HTTPException(status_code=422, detail="`name` is empty.")
    stored = []
    tpl_id = uuid.uuid4().hex[:10]
    tpl_dir = TPL_DIR / tpl_id
    tpl_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        data = await f.read()
        if not data:
            continue
        fname = Path(f.filename or "template").name
        (tpl_dir / fname).write_bytes(data)
        stored.append(fname)
    if not stored:
        shutil.rmtree(tpl_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail="No non-empty files were uploaded.")
    (tpl_dir / "meta.json").write_text(json.dumps({
        "name": name.strip(), "files": stored, "created_at": time.time(),
    }))
    return {"template_id": tpl_id, "name": name.strip(), "files": stored}


@app.delete("/templates/{template_id}")
async def delete_template(template_id: str) -> dict:
    tpl_dir = TPL_DIR / template_id
    if not (tpl_dir / "meta.json").exists():
        raise HTTPException(status_code=404, detail="Unknown template_id.")
    shutil.rmtree(tpl_dir, ignore_errors=True)
    return {"deleted": template_id}


# ------------------------------------------------------------------ status

@app.get("/podcasts/{job_id}")
async def get_podcast(job_id: str) -> dict:
    job = _get_job(job_id)
    script = job["script_obj"]
    deck = job["deck_obj"]
    body = {
        "job_id": job_id,
        "status": job["status"],
        "step": job["step"],
        "mode": job["mode"],
        "source": job["source"],
        "title": job["title"],
        "error": job["error"],
        "stage": _stage(job),
        "stale": job["stale"],
        "revisions": job["revisions"],
    }
    if script is not None:
        body["script"] = [
            {"line": i, "speaker": l.speaker, "text": l.text}
            for i, l in enumerate(script.lines)
        ]
    if deck is not None:
        assets_dir = Path(job["job_dir"]) / "assets"
        starts = (
            slides_mod.slide_times(deck, job["cues"], job["timing"])
            if job["cues"] else None
        )
        body["slides_deck"] = [
            {
                "title": s.title,
                "bullets": s.bullets,
                "statement": s.big_statement,
                "first_line": s.first_line,
                "start": starts[i] if starts else None,
                "offset": job["timing"].get(i, 0),
                "image": s.image,
                "figure": bool(s.figure_svg),
                "visual_url": (
                    f"/podcasts/{job_id}/assets/{vp.name}"
                    if (vp := slides_mod.slide_visual_path(s, i, assets_dir)) else None
                ),
            }
            for i, s in enumerate(deck.slides)
        ]
    if job.get("assets"):
        body["assets"] = [
            f"/podcasts/{job_id}/assets/{n}" for n in job["assets"]
        ]
    if job["delivery"]:
        body["voice_delivery"] = job["delivery"]
    if job["template_names"]:
        body["templates"] = job["template_names"]
    if job["theme_obj"] is not None:
        body["theme"] = job["theme_obj"].model_dump()
    if job["audio_path"]:
        body["audio_url"] = f"/podcasts/{job_id}/audio"
        body["transcript_url"] = f"/podcasts/{job_id}/transcript"
    if job["slides_path"]:
        body["slides_url"] = f"/podcasts/{job_id}/slides"
    if job["video_path"]:
        body["video_url"] = f"/podcasts/{job_id}/video"
    if job["pptx_path"]:
        body["pptx_url"] = f"/podcasts/{job_id}/pptx"
    return body


def _stage(job: dict) -> str:
    if job["audio_path"]:
        return "full"
    if job["deck_obj"] is not None:
        return "slides"
    if job["script_obj"] is not None:
        return "script"
    return "extracting"


# ------------------------------------------------------------------ revise

@app.post("/podcasts/{job_id}/revise", status_code=202)
async def revise(
    job_id: str,
    target: str = Form(..., description="What to revise: script, slides, or voice"),
    instructions: str = Form(..., description="Revision instructions; for "
                             "target=voice, reference transcript lines by number"),
) -> dict:
    job = _get_job(job_id)
    _require_idle(job)
    target = target.lower().strip()
    if target not in REVISE_TARGETS:
        raise HTTPException(
            status_code=422, detail="`target` must be one of: script, slides, voice."
        )
    if job["script_obj"] is None:
        raise HTTPException(status_code=409, detail="No script yet — nothing to revise.")
    if not instructions.strip():
        raise HTTPException(status_code=422, detail="`instructions` is empty.")

    job["status"] = "queued"
    job["error"] = None
    task = asyncio.create_task(_revise_task(job_id, target, instructions.strip()))
    job["_task"] = task
    return {"job_id": job_id, "status": "queued", "target": target,
            "status_url": f"/podcasts/{job_id}"}


async def _revise_task(job_id: str, target: str, instructions: str) -> None:
    job = jobs[job_id]
    try:
        job["status"] = "running"
        script = job["script_obj"]
        opts = job["options"]

        if target == "script":
            job["step"] = "revising_script"
            revised = await script_gen.revise_script(
                script, instructions, job["source_text"]
            )
            job["script_obj"] = revised
            job["title"] = revised.title
            # Line numbering changed: prior voice direction and timing no
            # longer apply; the deck may reference moved lines.
            job["delivery"] = {}
            job["cues"] = None
            if job["deck_obj"] is not None:
                job["stale"]["slides"] = True
            if job["audio_path"]:
                job["stale"]["audio"] = True
                job["stale"]["outputs"] = True

        elif target == "slides":
            if opts["slides"] is None:
                opts["slides"] = ["web"]
            job["step"] = "revising_slides"
            style = _effective_slide_style(job)
            assets = _job_assets(job)
            if job["deck_obj"] is None:
                job["deck_obj"] = await slides_mod.generate_slide_deck(
                    script, style, job["source_text"], assets, job["theme_obj"]
                )
                if instructions:
                    job["deck_obj"] = await slides_mod.revise_deck(
                        job["deck_obj"], script, instructions,
                        style, job["source_text"], assets, job["theme_obj"],
                    )
            else:
                job["deck_obj"] = await slides_mod.revise_deck(
                    job["deck_obj"], script, instructions,
                    style, job["source_text"], assets, job["theme_obj"],
                )
            _refresh_figure_assets(job)
            job["timing"] = {}  # slide indices may have shifted
            job["stale"]["slides"] = False
            # Re-render visual outputs immediately if audio already exists.
            if job["audio_path"] and not job["stale"]["audio"]:
                await _render_outputs(job_id)
            else:
                job["stale"]["outputs"] = True

        else:  # voice
            job["step"] = "planning_voice"
            plan = await script_gen.plan_voice(
                script, job["delivery"], instructions, job["cues"]
            )
            for change in plan.changes:
                if not 0 <= change.line < len(script.lines):
                    continue
                d = job["delivery"].setdefault(change.line, {})
                for field in ("spoken", "rate", "pitch", "volume", "voice"):
                    value = getattr(change, field)
                    if value is not None:
                        d[field] = value
            # Hear the result: re-synthesize and refresh outputs.
            await _render_audio(job_id)
            if job["deck_obj"] is not None and opts["slides"]:
                await _render_outputs(job_id)

        job["revisions"].append({"target": target, "instructions": instructions})
        _save_state(job)
        job["step"] = None
        job["status"] = "done"
    except Exception as e:
        # Keep prior artifacts usable; surface the failure in `error`.
        job["step"] = None
        job["status"] = "done"
        job["error"] = f"revision failed — {type(e).__name__}: {e}"


# ------------------------------------------------------------------ timing

@app.post("/podcasts/{job_id}/timing", status_code=202)
async def adjust_timing(
    job_id: str,
    offsets: str = Form(
        ...,
        description='JSON object of per-slide offsets in seconds, e.g. '
        '{"4": -2, "7": 1.5}. Slide indices are 0-based; 0 clears an offset.',
    ),
) -> dict:
    """Nudge when slides appear relative to the narration, then re-render
    the slideshow/video (audio is untouched)."""
    job = _get_job(job_id)
    _require_idle(job)
    if job["deck_obj"] is None:
        raise HTTPException(status_code=409, detail="No slide deck yet.")

    try:
        parsed = json.loads(offsets)
        assert isinstance(parsed, dict)
        parsed = {int(k): float(v) for k, v in parsed.items()}
    except (ValueError, AssertionError):
        raise HTTPException(
            status_code=422,
            detail='`offsets` must be a JSON object like {"4": -2, "7": 1.5}.',
        )
    n = len(job["deck_obj"].slides)
    for k, v in parsed.items():
        if not 0 <= k < n:
            raise HTTPException(status_code=422, detail=f"No slide index {k}.")
        if abs(v) > 120:
            raise HTTPException(status_code=422, detail="Offsets over ±120s rejected.")

    for k, v in parsed.items():
        if v == 0:
            job["timing"].pop(k, None)
        else:
            job["timing"][k] = v

    job["status"] = "queued"
    job["error"] = None
    task = asyncio.create_task(_timing_task(job_id))
    job["_task"] = task
    return {"job_id": job_id, "status": "queued", "timing": job["timing"],
            "status_url": f"/podcasts/{job_id}"}


async def _timing_task(job_id: str) -> None:
    job = jobs[job_id]
    try:
        job["status"] = "running"
        if job["audio_path"] and job["options"]["slides"] and not job["stale"]["audio"]:
            await _render_outputs(job_id)
        _save_state(job)
        job["step"] = None
        job["status"] = "done"
    except Exception as e:
        job["step"] = None
        job["status"] = "done"
        job["error"] = f"timing update failed — {type(e).__name__}: {e}"


# ------------------------------------------------------------------ render

@app.post("/podcasts/{job_id}/render", status_code=202)
async def render(
    job_id: str,
    slides: Optional[str] = Form(None),
    slide_style: Optional[str] = Form(None),
    captions: Optional[str] = Form(None),
) -> dict:
    """(Re)produce voice-over + outputs from the current script/deck/delivery.

    Optionally updates the slides/captions/slide_style options first.
    """
    job = _get_job(job_id)
    _require_idle(job)
    if job["script_obj"] is None:
        raise HTTPException(status_code=409, detail="No script yet — nothing to render.")

    opts = job["options"]
    if slides is not None:
        opts["slides"] = _validate_slides(slides)
    if slide_style is not None:
        opts["slide_style"] = slide_style
    if captions is not None:
        opts["captions"] = _validate_captions(captions, opts["slides"])

    job["status"] = "queued"
    job["error"] = None
    task = asyncio.create_task(_render_task(job_id))
    job["_task"] = task
    return {"job_id": job_id, "status": "queued", "status_url": f"/podcasts/{job_id}"}


async def _render_task(job_id: str) -> None:
    job = jobs[job_id]
    try:
        job["status"] = "running"
        opts = job["options"]

        if opts["slides"] and (job["deck_obj"] is None or job["stale"]["slides"]):
            job["step"] = "designing_slides"
            job["deck_obj"] = await slides_mod.generate_slide_deck(
                job["script_obj"], _effective_slide_style(job), job["source_text"],
                _job_assets(job), job["theme_obj"],
            )
            _refresh_figure_assets(job)
            job["timing"] = {}
            job["stale"]["slides"] = False

        await _render_audio(job_id)
        if opts["slides"]:
            await _render_outputs(job_id)

        _save_state(job)
        job["step"] = None
        job["status"] = "done"
    except Exception as e:
        job["step"] = None
        job["status"] = "done"
        job["error"] = f"render failed — {type(e).__name__}: {e}"


# ------------------------------------------------------ pipeline internals

async def _create_pipeline(
    job_id: str,
    until: str,
    url: Optional[str],
    pdf_bytes: Optional[bytes],
    pdf_name: Optional[str],
) -> None:
    job = jobs[job_id]
    opts = job["options"]
    try:
        job["status"] = "running"

        job["step"] = "extracting"
        if url is not None:
            title, text, images = await extract.extract_from_url(url)
        else:
            title, text, images = await asyncio.to_thread(
                extract.extract_from_pdf, pdf_bytes, pdf_name
            )
        job["title"] = title
        job["source_text"] = text
        assets_dir = Path(job["job_dir"]) / "assets"
        if images:
            assets_dir.mkdir(exist_ok=True)
            for name, data in images:
                (assets_dir / name).write_bytes(data)
        job["assets"] = [name for name, _ in images]

        if job["_template_digests"]:
            job["step"] = "reading_templates"
            job["theme_obj"] = await slides_mod.derive_theme(job["_template_digests"])

        job["step"] = "writing_script"
        mode = job["mode"]
        if mode == "podcast":
            script = await script_gen.generate_podcast_script(
                title, text, opts["host_a"], opts["host_b"], opts["audience"]
            )
        elif mode == "summary":
            script = await script_gen.generate_summary(
                title, text, opts["reader"], opts["audience"]
            )
        else:
            script = await script_gen.generate_readout(
                title, text, opts["reader"], opts["audience"]
            )
        job["script_obj"] = script
        job["title"] = script.title

        if until == "script":
            _save_state(job)
            job["step"] = None
            job["status"] = "done"
            return

        if opts["slides"]:
            job["step"] = "designing_slides"
            job["deck_obj"] = await slides_mod.generate_slide_deck(
                script, _effective_slide_style(job), text, _job_assets(job),
                job["theme_obj"],
            )
            _refresh_figure_assets(job)

        if until == "slides":
            _save_state(job)
            job["step"] = None
            job["status"] = "done"
            return

        await _render_audio(job_id)
        if opts["slides"]:
            await _render_outputs(job_id)

        _save_state(job)
        job["step"] = None
        job["status"] = "done"
    except ExtractionError as e:
        job["status"] = "failed"
        job["step"] = None
        job["error"] = str(e)
    except Exception as e:
        job["status"] = "failed"
        job["step"] = None
        job["error"] = f"{type(e).__name__}: {e}"


async def _render_audio(job_id: str) -> None:
    """Synthesize voice-over (honoring delivery direction) + transcript."""
    job = jobs[job_id]
    script = job["script_obj"]

    job["step"] = "synthesizing_audio"
    audio_path = Path(job["job_dir"]) / "audio.mp3"
    cues = await tts.synthesize(
        script.lines, audio_path, job["options"]["voices"], job["delivery"]
    )
    job["audio_path"] = str(audio_path)
    job["cues"] = cues
    job["stale"]["audio"] = False

    transcript_path = Path(job["job_dir"]) / "transcript.txt"
    transcript_path.write_text(
        text_out.build_transcript(
            script.title, job["source"], job["mode"], script.lines, cues
        ),
        encoding="utf-8",
    )
    job["transcript_path"] = str(transcript_path)


async def _render_outputs(job_id: str) -> None:
    """Render the web slideshow (and video) from current deck + audio."""
    job = jobs[job_id]
    script = job["script_obj"]
    deck = job["deck_obj"]
    opts = job["options"]
    cues = job["cues"]
    audio_path = Path(job["audio_path"])

    times = slides_mod.slide_times(deck, cues, job["timing"])
    total = cues[-1]["start"] + cues[-1]["duration"] if cues else 0.0

    caption_chunks = None
    srt_path = None
    if opts["captions"]:
        caption_chunks = text_out.build_caption_chunks(script.lines, cues)
        srt_path = Path(job["job_dir"]) / "captions.srt"
        srt_path.write_text(text_out.build_srt(caption_chunks), encoding="utf-8")

    job["step"] = "rendering_slides"
    slides_path = Path(job["job_dir"]) / "slides.html"
    slides_mod.render_html(
        script.title, deck, times, audio_path, slides_path,
        captions=caption_chunks,
        captions_default_on=(opts["captions"] == "burned"),
        theme=job["theme_obj"],
        assets_dir=Path(job["job_dir"]) / "assets",
    )
    job["slides_path"] = str(slides_path)

    if "pptx" in opts["slides"]:
        job["step"] = "rendering_pptx"
        pptx_path = Path(job["job_dir"]) / "slides.pptx"
        await asyncio.to_thread(
            pptx_out.build_pptx, script.title, deck, script, job["theme_obj"],
            Path(job["job_dir"]) / "assets", pptx_path,
        )
        job["pptx_path"] = str(pptx_path)

    if "video" in opts["slides"]:
        job["step"] = "rendering_video"
        video_path = Path(job["job_dir"]) / "video.mp4"
        await slides_mod.render_video(
            deck, times, total, audio_path, video_path,
            srt_path=srt_path,
            caption_chunks=caption_chunks,
            burn_captions=(opts["captions"] == "burned"),
            theme=job["theme_obj"],
            assets_dir=Path(job["job_dir"]) / "assets",
        )
        job["video_path"] = str(video_path)
    job["stale"]["outputs"] = False


# ------------------------------------------------------------------ files

@app.get("/podcasts/{job_id}/assets/{name}")
async def get_asset(job_id: str, name: str) -> FileResponse:
    job = _get_job(job_id)
    assets_dir = (Path(job["job_dir"]) / "assets").resolve()
    path = (assets_dir / name).resolve()
    if not path.is_relative_to(assets_dir) or not path.exists():
        raise HTTPException(status_code=404, detail="Unknown asset.")
    media = {"png": "image/png", "svg": "image/svg+xml"}.get(
        path.suffix.lstrip("."), "image/jpeg")
    return FileResponse(path, media_type=media)


@app.get("/podcasts/{job_id}/audio")
async def get_audio(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    _require_artifact(job, "audio_path")
    return FileResponse(
        job["audio_path"], media_type="audio/mpeg",
        filename=f"{_safe_title(job)}.mp3",
    )


@app.get("/podcasts/{job_id}/transcript")
async def get_transcript(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    _require_artifact(job, "transcript_path")
    return FileResponse(
        job["transcript_path"], media_type="text/plain",
        filename=f"{_safe_title(job)}.txt",
    )


@app.get("/podcasts/{job_id}/slides")
async def get_slides(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    _require_artifact(job, "slides_path",
                      hint=" (request it with slides=web or slides=video)")
    return FileResponse(job["slides_path"], media_type="text/html")


@app.get("/podcasts/{job_id}/pptx")
async def get_pptx(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    _require_artifact(job, "pptx_path", hint=" (request it with slides=pptx)")
    return FileResponse(
        job["pptx_path"],
        media_type="application/vnd.openxmlformats-officedocument"
        ".presentationml.presentation",
        filename=f"{_safe_title(job)}.pptx",
    )


@app.get("/podcasts/{job_id}/video")
async def get_video(job_id: str) -> FileResponse:
    job = _get_job(job_id)
    _require_artifact(job, "video_path", hint=" (request it with slides=video)")
    return FileResponse(
        job["video_path"], media_type="video/mp4",
        filename=f"{_safe_title(job)}.mp4",
    )


# ------------------------------------------------------------------ helpers

def _job_assets(job: dict) -> list[tuple[str, Path]]:
    assets_dir = Path(job["job_dir"]) / "assets"
    return [
        (n, assets_dir / n) for n in job.get("assets", [])
        if (assets_dir / n).exists()
    ]


def _refresh_figure_assets(job: dict) -> None:
    if job["deck_obj"] is not None:
        slides_mod.render_figure_assets(
            job["deck_obj"], Path(job["job_dir"]) / "assets"
        )


def _effective_slide_style(job: dict) -> Optional[str]:
    """User slide_style combined with content guidelines from the templates."""
    parts = [job["options"]["slide_style"]]
    theme = job["theme_obj"]
    if theme is not None and theme.content_guidelines:
        parts.append(f"From the provided templates: {theme.content_guidelines}")
    combined = " ".join(p for p in parts if p)
    return combined or None


def _save_state(job: dict) -> None:
    """Mirror the job's current state into its data/ subfolder as JSON."""
    job_dir = Path(job["job_dir"])
    if job["script_obj"] is not None:
        (job_dir / "script.json").write_text(
            json.dumps(
                {
                    "title": job["script_obj"].title,
                    "lines": [
                        {"line": i, "speaker": l.speaker, "text": l.text}
                        for i, l in enumerate(job["script_obj"].lines)
                    ],
                    "voice_delivery": job["delivery"],
                },
                indent=1,
            ),
            encoding="utf-8",
        )
    if job["deck_obj"] is not None:
        (job_dir / "deck.json").write_text(
            json.dumps([s.model_dump() for s in job["deck_obj"].slides], indent=1),
            encoding="utf-8",
        )
    if job["theme_obj"] is not None:
        (job_dir / "theme.json").write_text(
            job["theme_obj"].model_dump_json(indent=1), encoding="utf-8"
        )
    if job["source_text"] and not (job_dir / "source.txt").exists():
        (job_dir / "source.txt").write_text(job["source_text"], encoding="utf-8")
    (job_dir / "meta.json").write_text(
        json.dumps(
            {
                "mode": job["mode"],
                "source": job["source"],
                "title": job["title"],
                "created_at": job.get("created_at"),
                "options": job["options"],
                "templates": job["template_names"],
                "revisions": job["revisions"],
                "cues": job["cues"],
                "timing": job["timing"],
                "stale": job["stale"],
            },
            indent=1,
        ),
        encoding="utf-8",
    )


def _get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown job id.")
    return job


def _require_idle(job: dict) -> None:
    if job["status"] in ("queued", "running"):
        raise HTTPException(
            status_code=409,
            detail=f"Job is busy (status: {job['status']}, step: {job['step']}); "
            "wait for it to finish.",
        )


def _require_artifact(job: dict, key: str, hint: str = "") -> None:
    if not job[key]:
        if job["status"] in ("queued", "running"):
            raise HTTPException(
                status_code=409, detail=f"Job is still {job['status']}."
            )
        raise HTTPException(
            status_code=404,
            detail=f"This artifact hasn't been generated{hint}. "
            "Use POST /podcasts/{id}/render to produce outputs.",
        )


def _safe_title(job: dict) -> str:
    return "".join(
        c if c.isalnum() or c in " -_" else "_" for c in (job["title"] or "podcast")
    ).strip() or "podcast"
