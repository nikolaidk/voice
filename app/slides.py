"""Slide deck generation and rendering (web slideshow / MP4 video).

The deck is designed by Claude against the finished script: each slide points
at the script line where it should appear. TTS timing cues then give each
slide an absolute start time in the audio.
"""

import asyncio
import base64
import html
import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

from pydantic import BaseModel

from .script_gen import MODEL, PodcastScript, client, record_usage


class Slide(BaseModel):
    title: str
    bullets: list[str]
    big_statement: str | None = None
    image: str | None = None       # asset filename copied from the source
    figure_svg: str | None = None  # LLM-designed figure (chart/diagram/timeline) as themed SVG
    first_line: int


class SlideDeck(BaseModel):
    slides: list[Slide]


class SlideTheme(BaseModel):
    """Visual theme + content rules derived from user-provided template files."""

    background: str        # page background, hex like "#16181d"
    panel: str             # slide surface, hex
    text_color: str        # primary text, hex
    muted_color: str       # secondary text, hex
    accent: str            # accent (bars, bullets), hex
    font_family: str       # CSS font stack for body text
    heading_font_family: str  # CSS font stack for slide titles
    content_guidelines: str | None = None  # content/tone rules implied by the templates


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULT_THEME = SlideTheme(
    background="#16181d",
    panel="#1e222a",
    text_color="#f2f0eb",
    muted_color="#9aa0ab",
    accent="#e8a13c",
    font_family='-apple-system, "Segoe UI", Helvetica, Arial, sans-serif',
    heading_font_family='-apple-system, "Segoe UI", Helvetica, Arial, sans-serif',
)


def _safe_hex(value: str, fallback: str) -> str:
    return value if _HEX_RE.match(value or "") else fallback


# ------------------------------------------------------- template ingestion

_TEXT_EXT = {".html", ".htm", ".css", ".md", ".txt", ".json", ".yaml", ".yml", ".svg"}
_IMAGE_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".webp": "image/webp", ".gif": "image/gif"}
_MAX_DIGEST_CHARS = 20_000


def digest_template(filename: str, data: bytes) -> dict:
    """Turn one template file into a content block for theme derivation.

    Returns {"kind": "text", "text": ...} or {"kind": "image", "media_type",
    "data_b64"}.
    """
    ext = Path(filename).suffix.lower()

    if ext in (".pptx", ".potx", ".ppt"):
        return {"kind": "text", "text": _pptx_digest(filename, data)}

    if ext in _IMAGE_MEDIA:
        return {
            "kind": "image",
            "media_type": _IMAGE_MEDIA[ext],
            "data_b64": base64.b64encode(data).decode(),
        }

    if ext == ".pdf":
        from pypdf import PdfReader

        pages = PdfReader(io.BytesIO(data)).pages[:10]
        text = "\n".join((p.extract_text() or "") for p in pages)
        return {
            "kind": "text",
            "text": f"--- Template file {filename} (PDF text) ---\n"
            + text[:_MAX_DIGEST_CHARS],
        }

    if ext in _TEXT_EXT:
        text = data.decode("utf-8", errors="ignore")
        return {
            "kind": "text",
            "text": f"--- Template file {filename} ---\n" + text[:_MAX_DIGEST_CHARS],
        }

    # Unknown format: try text, otherwise note that it was skipped.
    try:
        text = data.decode("utf-8")
        return {
            "kind": "text",
            "text": f"--- Template file {filename} ---\n" + text[:_MAX_DIGEST_CHARS],
        }
    except UnicodeDecodeError:
        return {
            "kind": "text",
            "text": f"--- Template file {filename}: unsupported binary format, "
            "could not be read ---",
        }


def _pptx_digest(filename: str, data: bytes) -> str:
    """Extract theme colors, fonts, and master text from a .pptx/.potx."""
    out = [f"--- Template file {filename} (PowerPoint theme) ---"]
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            theme_files = sorted(
                n for n in z.namelist() if re.match(r"ppt/theme/theme\d+\.xml$", n)
            )
            for name in theme_files[:2]:
                xml = z.read(name).decode("utf-8", errors="ignore")
                colors = re.findall(
                    r"<a:(dk1|lt1|dk2|lt2|accent[1-6]|hlink)>\s*<a:(?:srgbClr "
                    r'val|sysClr[^>]*lastClr)="([0-9A-Fa-f]{6})"',
                    xml,
                )
                if colors:
                    out.append(
                        "Theme colors: "
                        + ", ".join(f"{k}=#{v}" for k, v in colors)
                    )
                fonts = re.findall(
                    r'<a:(majorFont|minorFont)>\s*<a:latin typeface="([^"]+)"', xml
                )
                if fonts:
                    out.append(
                        "Theme fonts: "
                        + ", ".join(f"{k}={v}" for k, v in fonts)
                    )
            # Any literal text on the master/layouts often carries style rules.
            layout_files = [
                n for n in z.namelist()
                if re.match(r"ppt/slide(Master|Layout)s?/[^/]+\.xml$", n)
            ]
            texts = set()
            for name in layout_files[:8]:
                xml = z.read(name).decode("utf-8", errors="ignore")
                texts.update(re.findall(r"<a:t>([^<]{3,80})</a:t>", xml))
            if texts:
                out.append("Master/layout text: " + "; ".join(sorted(texts)[:20]))
    except zipfile.BadZipFile:
        out.append("(file could not be parsed as a PowerPoint archive)")
    return "\n".join(out)


_THEME_SYSTEM = """You derive a slide-deck visual theme from template \
materials a user provided (PowerPoint theme extracts, HTML/CSS, style-guide \
documents, or screenshots of slides they like).

Produce:
- Colors as 6-digit hex (#rrggbb): background (page behind the slide), panel \
(the slide surface itself — often slightly lighter/darker than background), \
text_color, muted_color (secondary text), accent (bars, bullets, highlights). \
Ensure readable contrast between text_color and panel. If the template is \
light, make the whole theme light.
- font_family and heading_font_family as CSS font stacks that approximate the \
template's fonts using widely available fonts (always end with a generic \
family).
- content_guidelines: a short paragraph of any content/tone rules the \
templates imply (bullet density, capitalization, wording style, do's and \
don'ts) — null if none are evident."""


async def derive_theme(digests: list[dict]) -> SlideTheme:
    content: list[dict] = []
    for d in digests:
        if d["kind"] == "image":
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": d["media_type"],
                        "data": d["data_b64"],
                    },
                }
            )
        else:
            content.append({"type": "text", "text": d["text"]})
    content.append(
        {"type": "text", "text": "Derive the slide theme from these templates now."}
    )

    response = await client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_THEME_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=SlideTheme,
    )
    record_usage(response)
    theme = response.parsed_output
    if theme is None:
        raise RuntimeError("Claude did not return a usable slide theme.")
    d = DEFAULT_THEME
    theme.background = _safe_hex(theme.background, d.background)
    theme.panel = _safe_hex(theme.panel, d.panel)
    theme.text_color = _safe_hex(theme.text_color, d.text_color)
    theme.muted_color = _safe_hex(theme.muted_color, d.muted_color)
    theme.accent = _safe_hex(theme.accent, d.accent)
    theme.font_family = theme.font_family or d.font_family
    theme.heading_font_family = theme.heading_font_family or theme.font_family
    return theme


_SYSTEM = """You design professional slide decks that accompany a narrated \
audio track (a podcast episode, spoken summary, or document readout).

You are given the narration script as numbered lines, and usually the original \
source material as well. Produce a deck that a listener watches while the \
audio plays.

Structural rules (always apply):
- Slide 1 is a title slide: the episode title, with `big_statement` as a short \
subtitle, no bullets, first_line 0.
- Each slide has a short `title` plus EITHER `bullets` OR one `big_statement` \
(for emphasis slides). Never both.
- `first_line` is the number of the script line at which the slide should \
appear. Values must be strictly increasing across the deck and spread across \
the whole script — don't bunch all slides at the start.
- End with a closing slide (takeaway or thanks) near the final lines.

Visuals (use them — a professional deck is not text-only):
- `image`: when image assets from the source are provided (each labeled with \
its exact filename), set `image` to that filename on slides where the picture \
genuinely supports the idea. Use only listed filenames; use each image at \
most once; null when nothing fits.
- `figure_svg`: design a figure yourself whenever it makes the point better \
than words — a chart of any form (bar, line, area, donut, slope, dot), a \
timeline, a process/flow diagram, a comparison graphic, a big-number stat. \
Emit a complete self-contained <svg> with viewBox="0 0 1000 620" and no \
width/height attributes. Every fact and number in a figure must come from \
the source — never invent or extrapolate data.
- Figure craft (follow strictly): use the THEME PALETTE provided; \
background transparent or the panel color; text in the theme text/muted \
colors with font-family="sans-serif" and font sizes ≥ 22 for labels; marks \
in the accent color (one hue — vary shade for multiple categories, never a \
rainbow); thin marks and 1px gridlines in the muted color at low opacity; \
label key data points directly instead of every point; no dual axes, no 3D, \
no <script>, no external references or embedded images.
- A slide with a figure keeps text minimal (≤2 bullets).
- At most ONE visual (image OR figure_svg) per slide. Not every slide needs \
one; the title slide gets an image only if one is clearly iconic.

Default density (unless the requester's expectations below say otherwise):
- 6 to 14 slides total (fewer for very short scripts).
- 2-5 bullets per slide, max ~8 words each, no full sentences, no trailing \
periods. Slides support what's being said; they don't transcribe it.__EXPECTATIONS__"""

_EXPECTATIONS_BLOCK = """

The requester's expectations for this deck: {style}
These expectations override the default density above. If they ask for simple \
or minimal slides, use fewer slides and fewer, shorter bullets. If they ask \
for slides MORE informative than the voice-over, add supporting detail drawn \
from the source material — figures, definitions, dates, names, short data \
points, concrete examples — even where the narration doesn't mention them \
(up to ~7 bullets, which may be short full sentences). Never contradict the \
narration or invent facts not in the source."""


def _asset_blocks(assets: list[tuple[str, Path]] | None) -> list[dict]:
    """Vision content blocks presenting each source image with its filename."""
    blocks: list[dict] = []
    if not assets:
        return blocks
    blocks.append({
        "type": "text",
        "text": "Available image assets from the source (reference by exact "
                "filename in slide.image):",
    })
    for name, path in assets:
        blocks.append({"type": "text", "text": f"Asset: {name}"})
        blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(path.read_bytes()).decode(),
            },
        })
    return blocks


def _palette_block(theme: SlideTheme | None) -> str:
    t = theme or DEFAULT_THEME
    return (
        f"THEME PALETTE for figures — panel: {t.panel}, text: {t.text_color}, "
        f"muted: {t.muted_color}, accent: {t.accent}."
    )


async def _parse_deck_with_retry(**kwargs):
    """One retry on malformed structured output — rare model glitches
    (e.g. out-of-range numbers in figure coordinates) fail JSON parsing."""
    from pydantic import ValidationError
    try:
        return await client.messages.parse(**kwargs)
    except ValidationError:
        return await client.messages.parse(**kwargs)


async def generate_slide_deck(
    script: PodcastScript,
    style: str | None = None,
    source_text: str | None = None,
    assets: list[tuple[str, Path]] | None = None,
    theme: SlideTheme | None = None,
) -> SlideDeck:
    numbered = "\n".join(
        f"{i}: [{line.speaker}] {line.text}" for i, line in enumerate(script.lines)
    )
    expectations = _EXPECTATIONS_BLOCK.format(style=style) if style else ""
    content: list[dict] = [
        {"type": "text", "text": f"Episode title: {script.title}"},
        {"type": "text", "text": _palette_block(theme)},
        {"type": "text", "text": f"Narration script:\n{numbered}"},
    ]
    content.extend(_asset_blocks(assets))
    if source_text:
        content.append({
            "type": "text",
            "text": "Original source material (for supporting detail and chart "
                    f"data beyond the narration):\n\n{source_text}",
        })
    content.append({"type": "text", "text": "Design the slide deck now."})

    response = await _parse_deck_with_retry(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_SYSTEM.replace("__EXPECTATIONS__", expectations),
        messages=[{"role": "user", "content": content}],
        output_format=SlideDeck,
    )
    record_usage(response)
    deck = response.parsed_output
    if deck is None or not deck.slides:
        raise RuntimeError("Claude did not return a usable slide deck.")
    return _sanitize(deck, len(script.lines), {n for n, _ in assets or []})


_REVISE_DECK_SYSTEM = """You revise an existing slide deck that accompanies a \
narrated audio track.

You are given the current deck, the narration script as numbered lines, and \
revision instructions. Apply the instructions precisely and only change what \
they require. Keep the structural rules: slide 1 is the title slide at \
first_line 0; each slide has a title plus EITHER bullets OR one \
big_statement; first_line values strictly increasing and spread across the \
script. Slides may carry an `image` (source asset filename) or a \
`figure_svg` (self-contained themed SVG, viewBox 0 0 1000 620, THEME \
PALETTE colors, data only from the source, no scripts/external refs). If \
the script changed since the deck was made, also fix any slides that no \
longer match it. Return the COMPLETE revised deck."""


async def revise_deck(
    deck: SlideDeck,
    script: PodcastScript,
    instructions: str,
    style: str | None = None,
    source_text: str | None = None,
    assets: list[tuple[str, Path]] | None = None,
    theme: SlideTheme | None = None,
) -> SlideDeck:
    numbered = "\n".join(
        f"{i}: [{line.speaker}] {line.text}" for i, line in enumerate(script.lines)
    )
    # Stable blocks first, cache breakpoint on the last of them: successive
    # deck revisions reuse the large source/assets/script prefix at ~10% of
    # input price. Only the current deck + instructions vary per iteration.
    content: list[dict] = [
        {"type": "text", "text": f"Episode title: {script.title}"},
        {"type": "text", "text": _palette_block(theme)},
    ]
    content.extend(_asset_blocks(assets))
    if source_text:
        content.append({"type": "text", "text": f"Original source material:\n\n{source_text}"})
    if style:
        content.append({"type": "text", "text": f"Standing expectations for the deck: {style}"})
    content.append({
        "type": "text",
        "text": f"Narration script:\n{numbered}",
        "cache_control": {"type": "ephemeral"},
    })
    content.append({"type": "text", "text": "Current deck:\n" + json.dumps(
        [s.model_dump() for s in deck.slides], indent=1)})
    content.append({"type": "text", "text": f"Revision instructions:\n{instructions}"})

    response = await _parse_deck_with_retry(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_REVISE_DECK_SYSTEM,
        messages=[{"role": "user", "content": content}],
        output_format=SlideDeck,
    )
    record_usage(response)
    revised = response.parsed_output
    if revised is None or not revised.slides:
        raise RuntimeError("Claude did not return a usable revised deck.")
    return _sanitize(revised, len(script.lines), {n for n, _ in assets or []})


_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape(text: str) -> str:
    r"""Models occasionally emit literal \uXXXX sequences in text fields."""
    return _ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _sanitize(
    deck: SlideDeck, n_lines: int, asset_names: set[str] | None = None
) -> SlideDeck:
    """Clamp line refs, enforce ascending order, validate visuals."""
    slides = sorted(deck.slides, key=lambda s: s.first_line)
    cleaned: list[Slide] = []
    prev = -1
    for s in slides:
        s.first_line = max(0, min(s.first_line, n_lines - 1))
        if s.first_line <= prev:
            s.first_line = prev + 1
            if s.first_line >= n_lines:
                continue
        s.title = _unescape(s.title)
        s.bullets = [_unescape(b) for b in s.bullets]
        if s.big_statement:
            s.big_statement = _unescape(s.big_statement)
        if s.image and s.image not in (asset_names or set()):
            s.image = None
        if s.figure_svg is not None:
            s.figure_svg = _clean_svg(s.figure_svg)
        if s.figure_svg is not None and s.image:
            s.image = None  # one visual per slide; the figure wins
        cleaned.append(s)
        prev = s.first_line
    if cleaned:
        cleaned[0].first_line = 0
    return SlideDeck(slides=cleaned)


def slide_times(
    deck: SlideDeck,
    cues: list[dict],
    timing: dict[int, float] | None = None,
) -> list[float]:
    """Absolute start time (s) of each slide, from per-line TTS cues.

    `timing` holds per-slide offsets in seconds ({slide_index: offset},
    negative = earlier). Slide 1 is pinned to 0 and order is preserved —
    offsets are clamped so a slide can't jump past its neighbors.
    """
    start_by_line = {c["line"]: c["start"] for c in cues}
    total = cues[-1]["start"] + cues[-1]["duration"] if cues else 0.0
    timing = timing or {}
    times: list[float] = []
    for i, slide in enumerate(deck.slides):
        line = slide.first_line
        while line not in start_by_line and line > 0:
            line -= 1
        t = start_by_line.get(line, 0.0) + timing.get(i, 0.0)
        if i == 0:
            t = 0.0
        else:
            t = max(t, times[-1] + 0.1)
            if total:
                t = min(t, max(total - 0.5, times[-1] + 0.1))
        times.append(round(t, 2))
    return times


# ------------------------------------------------------------------- figures

_SVG_FORBIDDEN = re.compile(
    r"<\s*script|<\s*foreignObject|javascript:|\bhref\s*=\s*[\"\']\s*https?:"
    r"|<\s*image|url\s*\(\s*https?:", re.I,
)


def _clean_svg(svg: str) -> str | None:
    """Validate an LLM-designed SVG figure; None if unusable or unsafe."""
    m = re.search(r"<svg[\s\S]*</svg>", svg or "")
    if not m:
        return None
    svg = m.group(0)
    if len(svg) > 100_000 or _SVG_FORBIDDEN.search(svg):
        return None
    head = svg.split(">", 1)[0]
    if "xmlns" not in head:
        svg = svg.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
    return svg


def _rasterize_svg(svg: str, out_path: Path) -> bool:
    """SVG -> PNG for the video renderer. Uses cairosvg; on macOS the cairo
    dylib lives under /opt/homebrew/lib, which cairocffi can't find on its
    own — point find_library at it during import."""
    try:
        import ctypes.util

        orig = ctypes.util.find_library

        def patched(name):
            if "cairo" in name:
                candidate = "/opt/homebrew/lib/libcairo.2.dylib"
                if Path(candidate).exists():
                    return candidate
            return orig(name)

        ctypes.util.find_library = patched
        try:
            import cairosvg
        finally:
            ctypes.util.find_library = orig
        cairosvg.svg2png(bytestring=svg.encode(), write_to=str(out_path),
                         output_width=1600)
        return True
    except Exception:
        return False


def render_figure_assets(deck: SlideDeck, assets_dir: Path) -> None:
    """Persist each slide's LLM-designed figure as .svg (web) + .png (video)."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    for old in list(assets_dir.glob("figure_*.svg")) + list(assets_dir.glob("figure_*.png")) \
            + list(assets_dir.glob("chart_*.png")):
        old.unlink()
    for i, slide in enumerate(deck.slides):
        if slide.figure_svg:
            svg_path = assets_dir / f"figure_{i:02d}.svg"
            svg_path.write_text(slide.figure_svg, encoding="utf-8")
            _rasterize_svg(slide.figure_svg, assets_dir / f"figure_{i:02d}.png")


def slide_visual_path(slide: Slide, index: int, assets_dir: Path,
                      raster: bool = False) -> Path | None:
    """Resolve the file backing a slide's visual (figure wins over image).

    raster=True returns the PNG variant of figures (for video frames);
    otherwise the crisp SVG (for the web player and workbench).
    """
    if slide.figure_svg:
        p = assets_dir / f"figure_{index:02d}.{'png' if raster else 'svg'}"
        return p if p.exists() else None
    if slide.image:
        p = assets_dir / slide.image
        return p if p.exists() else None
    return None


# ---------------------------------------------------------------- web format

def _visual_data_uri(path: Path) -> str:
    """Embed a visual as a data URI; photos re-encoded to keep the HTML lean."""
    if path.suffix == ".svg":
        return "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode()
    if path.suffix == ".png":
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    from PIL import Image

    img = Image.open(path).convert("RGB")
    if img.width > 900:
        img = img.resize((900, round(img.height * 900 / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def render_html(
    title: str,
    deck: SlideDeck,
    times: list[float],
    audio_path: Path,
    out_path: Path,
    captions: list[dict] | None = None,
    captions_default_on: bool = False,
    theme: SlideTheme | None = None,
    assets_dir: Path | None = None,
    animations: bool = False,
    footer: str | None = None,
) -> None:
    theme = theme or DEFAULT_THEME
    audio_b64 = base64.b64encode(audio_path.read_bytes()).decode()
    slide_dicts = []
    for i, (s, t) in enumerate(zip(deck.slides, times)):
        visual = None
        if assets_dir is not None:
            vp = slide_visual_path(s, i, assets_dir)
            if vp is not None:
                visual = _visual_data_uri(vp)
        slide_dicts.append({
            "title": s.title,
            "bullets": s.bullets,
            "statement": s.big_statement,
            "visual": visual,
            "start": round(t, 2),
        })
    slides_json = json.dumps(slide_dicts)
    page = _HTML_TEMPLATE.replace("__TITLE__", html.escape(title))
    page = page.replace("__SLIDES__", slides_json)
    page = page.replace("__CAPS__", json.dumps(captions or []))
    page = page.replace("__CAPSON__", "true" if captions_default_on else "false")
    page = page.replace("__BG__", theme.background)
    page = page.replace("__PANEL__", theme.panel)
    page = page.replace("__INK__", theme.text_color)
    page = page.replace("__MUTED__", theme.muted_color)
    page = page.replace("__ACCENT__", theme.accent)
    page = page.replace("__FONT__", theme.font_family)
    page = page.replace("__HEADFONT__", theme.heading_font_family)
    page = page.replace("__STAMP__", footer or "")
    page = page.replace("__ANIM__", "true" if animations else "false")
    page = page.replace("__AUDIO__", audio_b64)
    out_path.write_text(page, encoding="utf-8")


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: __BG__; --panel: __PANEL__; --ink: __INK__; --muted: __MUTED__;
    --accent: __ACCENT__;
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    background: var(--bg); color: var(--ink); height: 100vh;
    display: flex; flex-direction: column;
    font-family: __FONT__;
  }
  h1 { font-family: __HEADFONT__; }
  main { flex: 1; display: flex; align-items: center; justify-content: center; padding: 4vmin; position: relative; }
  .slide {
    width: min(92vw, 150vh); aspect-ratio: 16/9; background: var(--panel);
    border-radius: 14px; padding: 6% 7%; display: flex; flex-direction: column;
    justify-content: center; box-shadow: 0 18px 60px rgba(0,0,0,.5);
    position: relative; overflow: hidden;
  }
  .slide::before {
    content: ""; position: absolute; inset: 0 auto 0 0; width: 7px;
    background: var(--accent);
  }
  .slide h1 { font-size: 4.6vmin; line-height: 1.15; letter-spacing: -.01em; }
  .slide.cover h1 { font-size: 6vmin; }
  .slide .statement {
    margin-top: 3vmin; font-size: 3.4vmin; color: var(--muted); line-height: 1.35;
  }
  .slide.cover .statement { color: var(--accent); }
  .slide ul { margin-top: 4vmin; list-style: none; }
  .slide li {
    font-size: 3vmin; line-height: 1.4; padding: 1.2vmin 0 1.2vmin 3.4vmin;
    position: relative; color: var(--ink); opacity: .88;
  }
  .slide li::before {
    content: ""; position: absolute; left: 0; top: 2.35vmin; width: 1.4vmin;
    height: 1.4vmin; border-radius: 50%; background: var(--accent);
  }
  .slide.dense h1 { font-size: 3.4vmin; }
  .slide.dense ul { margin-top: 2.4vmin; overflow-y: auto; }
  .slide.dense li { font-size: 2.2vmin; padding: 0.8vmin 0 0.8vmin 3vmin; }
  .slide.dense li::before { top: 1.7vmin; width: 1.1vmin; height: 1.1vmin; }
  .slide-body { display: flex; gap: 4%; align-items: center; flex: 1; min-height: 0; }
  .slide-text { flex: 1; min-width: 0; }
  .slide-visual { flex: 0 0 44%; display: flex; align-items: center; justify-content: center; min-height: 0; max-height: 100%; }
  .slide-visual img { max-width: 100%; max-height: 52vmin; border-radius: 10px; object-fit: contain; }
  .slide.has-visual h1 { font-size: 3.8vmin; }
  .slide.has-visual li { font-size: 2.6vmin; }
  footer {
    display: flex; gap: 14px; align-items: center; padding: 14px 20px 20px;
    max-width: 1100px; width: 100%; margin: 0 auto;
  }
  audio { flex: 1; height: 40px; }
  button {
    background: var(--panel); color: var(--ink); border: 1px solid var(--muted);
    border-radius: 8px; width: 42px; height: 40px; font-size: 18px; cursor: pointer;
  }
  button:hover { border-color: var(--accent); }
  #counter { color: var(--muted); font-variant-numeric: tabular-nums; min-width: 56px; text-align: center; }
  #bar { position: fixed; top: 0; left: 0; height: 3px; background: var(--accent); width: 0; }
  #cap {
    position: absolute; left: 50%; bottom: 4%; transform: translateX(-50%);
    max-width: 86%; background: rgba(0,0,0,.62); color: #fff;
    padding: 1.2vmin 2vmin; border-radius: 8px; font-size: 2.5vmin;
    line-height: 1.35; text-align: center; display: none;
  }
  #cc.active { border-color: var(--accent); color: var(--accent); }
  #stamp { font-family: var(--mono, monospace); font-size: 11px; color: var(--muted);
    letter-spacing: .06em; opacity: .8; }
  #stamp:empty { display: none; }
  @keyframes aIn { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: none; } }
  .a-in { animation: aIn .55s cubic-bezier(.2,.7,.3,1) both; }
</style>
</head>
<body>
<div id="bar"></div>
<main><div class="slide" id="slide"></div><div id="cap"></div></main>
<footer>
  <button id="prev" title="Previous slide">&#8249;</button>
  <audio id="audio" controls src="data:audio/mpeg;base64,__AUDIO__"></audio>
  <button id="cc" title="Toggle captions" style="width:auto;padding:0 12px">CC</button>
  <button id="next" title="Next slide">&#8250;</button>
  <span id="counter"></span>
  <span id="stamp">__STAMP__</span>
</footer>
<script>
const SLIDES = __SLIDES__;
const CAPS = __CAPS__;
const ANIM = __ANIM__;
let capsOn = __CAPSON__;
const audio = document.getElementById("audio");
const slideEl = document.getElementById("slide");
const counter = document.getElementById("counter");
const bar = document.getElementById("bar");
let current = -1;

function esc(s) { const d = document.createElement("div"); d.textContent = s; return d.innerHTML; }

function show(i) {
  if (i === current) return;
  current = i;
  const s = SLIDES[i];
  let text = "<h1>" + esc(s.title) + "</h1>";
  if (s.statement) text += '<div class="statement">' + esc(s.statement) + "</div>";
  if (s.bullets && s.bullets.length)
    text += "<ul>" + s.bullets.map(b => "<li>" + esc(b) + "</li>").join("") + "</ul>";
  let inner = text;
  if (s.visual) {
    inner = '<div class="slide-body"><div class="slide-text">' + text +
      '</div><div class="slide-visual"><img src="' + s.visual + '" alt=""></div></div>';
  }
  const chars = (s.bullets || []).join("").length;
  const dense = (s.bullets || []).length > 5 || chars > 260;
  slideEl.className = "slide" + (i === 0 ? " cover" : "") +
    (dense ? " dense" : "") + (s.visual ? " has-visual" : "");
  slideEl.innerHTML = inner;
  if (ANIM) {
    slideEl.querySelectorAll("h1, .statement, .rv-st, li, .slide-visual img")
      .forEach((el, k) => {
        el.classList.add("a-in");
        el.style.animationDelay = (0.08 + k * 0.14).toFixed(2) + "s";
      });
  }
  counter.textContent = (i + 1) + " / " + SLIDES.length;
}

function slideAt(t) {
  let i = 0;
  for (let k = 0; k < SLIDES.length; k++) if (t >= SLIDES[k].start - 0.05) i = k;
  return i;
}

const capEl = document.getElementById("cap");
const ccBtn = document.getElementById("cc");
if (!CAPS.length) ccBtn.style.display = "none";
ccBtn.classList.toggle("active", capsOn);
ccBtn.onclick = () => { capsOn = !capsOn; ccBtn.classList.toggle("active", capsOn); updateCap(); };

function updateCap() {
  if (!capsOn) { capEl.style.display = "none"; return; }
  const t = audio.currentTime;
  const c = CAPS.find(c => t >= c.start && t < c.end);
  capEl.style.display = c ? "block" : "none";
  if (c) capEl.textContent = c.text;
}

audio.addEventListener("timeupdate", () => {
  show(slideAt(audio.currentTime));
  updateCap();
  if (audio.duration) bar.style.width = (100 * audio.currentTime / audio.duration) + "%";
});
document.getElementById("prev").onclick = () => { audio.currentTime = SLIDES[Math.max(0, current - 1)].start + 0.06; };
document.getElementById("next").onclick = () => { if (current < SLIDES.length - 1) audio.currentTime = SLIDES[current + 1].start + 0.06; };
document.addEventListener("keydown", e => {
  if (e.key === "ArrowLeft") document.getElementById("prev").click();
  if (e.key === "ArrowRight") document.getElementById("next").click();
  if (e.key === " ") { e.preventDefault(); audio.paused ? audio.play() : audio.pause(); }
});
show(0);
</script>
</body>
</html>
"""


# -------------------------------------------------------------- video format

_W, _H = 1280, 720


def _hex_rgb(h: str) -> tuple[int, int, int]:
    return tuple(int(h[i : i + 2], 16) for i in (1, 3, 5))

_FONT_CANDIDATES = [
    "/System/Library/Fonts/HelveticaNeue.ttc",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


async def render_video(
    deck: SlideDeck,
    times: list[float],
    total_duration: float,
    audio_path: Path,
    out_path: Path,
    srt_path: Path | None = None,
    caption_chunks: list[dict] | None = None,
    burn_captions: bool = False,
    theme: SlideTheme | None = None,
    assets_dir: Path | None = None,
    animations: bool = False,
    footer: str | None = None,
) -> None:
    if not ffmpeg_available():
        raise RuntimeError(
            "Video rendering requires ffmpeg (install with `brew install ffmpeg`); "
            "the web slideshow at /slides works without it."
        )
    await asyncio.to_thread(
        _render_video_sync, deck, times, total_duration, audio_path, out_path,
        srt_path, caption_chunks, burn_captions, theme, assets_dir, animations,
        footer,
    )


def _segments(times, total, chunks):
    """Timeline of (slide_idx, caption_text|None, duration) covering [0, total).

    Burned captions change more often than slides, so frames are emitted per
    (slide, caption) interval rather than per slide.
    """
    bounds = {0.0}
    bounds.update(t for t in times if 0 <= t < total)
    for c in chunks:
        for t in (c["start"], c["end"]):
            if 0 <= t < total:
                bounds.add(t)
    ordered = sorted(bounds)
    segs = []
    for a, b in zip(ordered, ordered[1:] + [total]):
        if b - a < 0.02:
            continue
        slide_i = 0
        for i, t in enumerate(times):
            if t <= a + 1e-6:
                slide_i = i
        cap = next(
            (c["text"] for c in chunks if c["start"] <= a + 1e-6 < c["end"]), None
        )
        segs.append((slide_i, cap, b - a))
    return segs


def _render_video_sync(deck, times, total_duration, audio_path, out_path,
                       srt_path=None, caption_chunks=None, burn_captions=False,
                       theme=None, assets_dir=None, animations=False, footer=None):
    from PIL import Image, ImageDraw, ImageFont

    theme = theme or DEFAULT_THEME
    bg = _hex_rgb(theme.panel)          # frames show the slide surface itself
    ink = _hex_rgb(theme.text_color)
    muted = _hex_rgb(theme.muted_color)
    accent = _hex_rgb(theme.accent)
    body_ink = tuple(round(i * 0.88 + b * 0.12) for i, b in zip(ink, bg))

    def font(size, bold=False):
        for path in _FONT_CANDIDATES:
            try:
                return ImageFont.truetype(path, size, index=1 if bold else 0)
            except OSError:
                continue
        return ImageFont.load_default()

    def _glyph_safe(t):
        """The video font lacks some symbols the model likes — substitute."""
        for bad, good in (("→", "->"), ("←", "<-"), ("↑", "^"), ("↓", "v"),
                          ("≈", "~"), ("≥", ">="), ("≤", "<="), ("✓", "+"),
                          ("×", "x"), ("•", "·")):
            t = t.replace(bad, good)
        return t

    def wrap(draw, text, fnt, max_w):
        text = _glyph_safe(text)
        words, lines, cur = text.split(), [], ""
        for w in words:
            trial = (cur + " " + w).strip()
            if draw.textlength(trial, font=fnt) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines

    def draw_slide(slide, i, scale, max_bullets=None):
        """Render one slide at the given font scale; returns (img, fits).

        max_bullets limits how many bullets are drawn — used for the
        animated build-in frames."""
        img = Image.new("RGB", (_W, _H), bg)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, 8, _H], fill=accent)

        visual = None
        if assets_dir is not None:
            vp = slide_visual_path(slide, i, assets_dir, raster=True)
            if vp is not None:
                try:
                    visual = Image.open(vp).convert("RGB")
                except OSError:
                    visual = None
        if visual is not None:
            # right-hand visual box; text column narrows to fit beside it
            box_w, box_h = round(_W * 0.42), _H - 170
            ratio = min(box_w / visual.width, box_h / visual.height)
            visual = visual.resize(
                (round(visual.width * ratio), round(visual.height * ratio)),
                Image.LANCZOS,
            )
            vx = _W - 70 - visual.width
            vy = max(90, (_H - 40 - visual.height) // 2)
            img.paste(visual, (vx, vy))

        is_cover = i == 0
        title_font = font(round((64 if is_cover else 48) * scale), bold=True)
        body_font = font(round((34 if visual is None else 30) * scale))
        st_font = font(round(40 * scale))
        x = 90
        max_w = (_W - 180) if visual is None else (_W - 200 - visual.width)
        gap = round(12 * scale)

        y = 130 if is_cover else 90
        for line in wrap(d, slide.title, title_font, max_w):
            d.text((x, y), line, font=title_font, fill=ink)
            y += title_font.size + gap
        y += round(28 * scale)

        if slide.big_statement:
            fill = accent if is_cover else muted
            for line in wrap(d, slide.big_statement, st_font, max_w):
                d.text((x, y), line, font=st_font, fill=fill)
                y += st_font.size + gap
        dot = round(14 * scale)
        bullets = slide.bullets if max_bullets is None else slide.bullets[:max_bullets]
        for bullet in map(_glyph_safe, bullets):
            d.ellipse([x, y + dot, x + dot, y + 2 * dot], fill=accent)
            for line in wrap(d, bullet, body_font, max_w - 40):
                d.text((x + 36, y), line, font=body_font, fill=body_ink)
                y += body_font.size + gap
            y += gap

        d.text(
            (_W - 90, _H - 60),
            f"{i + 1} / {len(deck.slides)}",
            font=font(24),
            fill=muted,
            anchor="ra",
        )
        if footer:
            d.text((90, _H - 60), footer, font=font(22), fill=muted, anchor="la")
        return img, y <= _H - 70

    def draw_caption(base, text):
        """Return a copy of a slide frame with a caption bar drawn on it."""
        img = base.convert("RGBA")
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(overlay)
        cap_font = font(28)
        max_w = _W - 320
        cap_lines = wrap(d, text, cap_font, max_w)
        line_h = cap_font.size + 8
        box_h = len(cap_lines) * line_h + 24
        widest = max(d.textlength(l, font=cap_font) for l in cap_lines)
        box_w = widest + 56
        x0 = (_W - box_w) / 2
        y0 = _H - box_h - 34
        d.rounded_rectangle(
            [x0, y0, x0 + box_w, y0 + box_h], radius=10, fill=(0, 0, 0, 168)
        )
        y = y0 + 12
        for l in cap_lines:
            d.text((_W / 2, y), l, font=cap_font, fill=(255, 255, 255, 255), anchor="ma")
            y += line_h
        return Image.alpha_composite(img, overlay).convert("RGB")

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        base_images = []
        base_scales = []
        for i, slide in enumerate(deck.slides):
            for scale in (1.0, 0.85, 0.72, 0.6, 0.5):
                img, fits = draw_slide(slide, i, scale)
                if fits:
                    break
            base_images.append(img)
            base_scales.append(scale)

        lines = ["ffconcat version 1.0"]
        if burn_captions and caption_chunks:
            # One frame per (slide, caption) interval, cached by content.
            frame_ids: dict[tuple, str] = {}
            for slide_i, cap, duration in _segments(
                times, total_duration, caption_chunks
            ):
                key = (slide_i, cap)
                if key not in frame_ids:
                    name = f"frame_{len(frame_ids):04d}.png"
                    frame = (
                        draw_caption(base_images[slide_i], cap)
                        if cap
                        else base_images[slide_i]
                    )
                    frame.save(tmp / name)
                    frame_ids[key] = name
                lines.append(f"file '{frame_ids[key]}'")
                lines.append(f"duration {duration:.3f}")
            lines.append(f"file '{frame_ids[key]}'")
        else:
            # One frame per slide — or, with animations, a short bullet
            # build-in: progressive frames revealing one bullet at a time.
            for i, img in enumerate(base_images):
                end = times[i + 1] if i + 1 < len(deck.slides) else total_duration
                duration = max(0.5, end - times[i])
                slide = deck.slides[i]
                n = len(slide.bullets)
                if animations and n >= 2 and duration > 3.0:
                    build_dt = min(0.7, (duration * 0.45) / n)
                    for k in range(n):
                        frame, _ = draw_slide(slide, i, base_scales[i], max_bullets=k)
                        frame.save(tmp / f"slide_{i:03d}_b{k:02d}.png")
                        lines.append(f"file 'slide_{i:03d}_b{k:02d}.png'")
                        lines.append(f"duration {build_dt:.3f}")
                    img.save(tmp / f"slide_{i:03d}.png")
                    lines.append(f"file 'slide_{i:03d}.png'")
                    lines.append(f"duration {duration - n * build_dt:.3f}")
                else:
                    img.save(tmp / f"slide_{i:03d}.png")
                    lines.append(f"file 'slide_{i:03d}.png'")
                    lines.append(f"duration {duration:.3f}")
            lines.append(f"file 'slide_{len(deck.slides) - 1:03d}.png'")
        (tmp / "list.txt").write_text("\n".join(lines))

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0", "-i", str(tmp / "list.txt"),
            "-i", str(audio_path),
        ]
        soft_subs = srt_path is not None and not burn_captions
        if soft_subs:
            cmd += ["-i", str(srt_path)]
        cmd += [
            "-c:v", "libx264", "-r", "12", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
        ]
        if soft_subs:
            # Toggleable subtitle track — viewers switch it on in the player.
            cmd += ["-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
        cmd += ["-shortest", str(out_path)]

        subprocess.run(cmd, check=True, capture_output=True)
