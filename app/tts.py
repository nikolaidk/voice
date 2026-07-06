"""Synthesize a script to a single MP3 with edge-tts."""

import io
import re
from pathlib import Path

import edge_tts
from mutagen.mp3 import MP3

from .script_gen import ScriptLine

DEFAULT_VOICES = {
    "HOST_A": "en-US-GuyNeural",
    "HOST_B": "en-US-AriaNeural",
    "NARRATOR": "en-US-GuyNeural",
}

_PCT_RE = re.compile(r"^[+-]\d{1,3}%$")
_HZ_RE = re.compile(r"^[+-]\d{1,3}Hz$")


def _delivery_kwargs(d: dict | None) -> dict:
    """Validated edge-tts prosody kwargs from a per-line delivery dict."""
    if not d:
        return {}
    kw = {}
    if d.get("rate") and _PCT_RE.match(d["rate"]):
        kw["rate"] = d["rate"]
    if d.get("volume") and _PCT_RE.match(d["volume"]):
        kw["volume"] = d["volume"]
    if d.get("pitch") and _HZ_RE.match(d["pitch"]):
        kw["pitch"] = d["pitch"]
    return kw


async def synthesize(
    lines: list[ScriptLine],
    out_path: Path,
    voices: dict[str, str] | None = None,
    delivery: dict[int, dict] | None = None,
) -> list[dict]:
    """Render each line with its speaker's voice and append into one MP3 stream.

    MP3 frames are self-contained, so sequential concatenation of the segments
    produces a single playable file without ffmpeg.

    `delivery` holds per-line overrides ({line_index: {spoken, rate, pitch,
    volume, voice}}) from voice-direction iterations.

    Returns timing cues: one {"line", "start", "duration"} dict per rendered
    line (seconds), used to sync slides/captions to the audio.
    """
    voice_map = {**DEFAULT_VOICES, **(voices or {})}
    delivery = delivery or {}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cues: list[dict] = []
    t = 0.0
    with open(out_path, "wb") as f:
        for i, line in enumerate(lines):
            d = delivery.get(i, {})
            text = (d.get("spoken") or line.text).strip()
            if not text:
                continue
            voice = d.get("voice") or voice_map[line.speaker]
            buf = io.BytesIO()
            communicate = edge_tts.Communicate(text, voice, **_delivery_kwargs(d))
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    buf.write(chunk["data"])
            data = buf.getvalue()
            duration = MP3(io.BytesIO(data)).info.length
            f.write(data)
            cues.append({"line": i, "start": t, "duration": duration})
            t += duration
    return cues
