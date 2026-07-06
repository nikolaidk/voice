"""Transcript and caption generation from the script + TTS timing cues."""

import re

from .script_gen import ScriptLine

SPEAKER_NAMES = {"HOST_A": "Host A", "HOST_B": "Host B", "NARRATOR": "Narrator"}

# Captions read best in short chunks; long narration lines are split by
# sentence and timed proportionally to their share of the line's audio.
_MAX_CAPTION_CHARS = 90


def build_transcript(
    title: str,
    source: str,
    mode: str,
    lines: list[ScriptLine],
    cues: list[dict],
) -> str:
    start_by_line = {c["line"]: c["start"] for c in cues}
    out = [title, f"Mode: {mode} | Source: {source}", ""]
    for i, line in enumerate(lines):
        text = line.text.strip()
        if not text:
            continue
        t = start_by_line.get(i, 0.0)
        stamp = f"[{int(t // 60):02d}:{int(t % 60):02d}]"
        out.append(f"{stamp} {SPEAKER_NAMES[line.speaker]}: {text}")
    return "\n".join(out) + "\n"


def build_caption_chunks(lines: list[ScriptLine], cues: list[dict]) -> list[dict]:
    """Per-caption {"start", "end", "text"} chunks (seconds)."""
    chunks: list[dict] = []
    for cue in cues:
        line = lines[cue["line"]]
        chunks.extend(_split_line(line.text.strip(), cue["start"], cue["duration"]))
    return chunks


def _split_line(text: str, start: float, duration: float) -> list[dict]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    parts: list[str] = []
    cur = ""
    for s in sentences:
        if cur and len(cur) + len(s) + 1 > _MAX_CAPTION_CHARS:
            parts.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        parts.append(cur)

    # Hard-split any single overlong sentence on a word boundary.
    final: list[str] = []
    for p in parts:
        while len(p) > _MAX_CAPTION_CHARS * 1.5:
            cut = p.rfind(" ", 0, _MAX_CAPTION_CHARS)
            if cut <= 0:
                break
            final.append(p[:cut])
            p = p[cut + 1 :]
        final.append(p)

    total_chars = sum(len(p) for p in final) or 1
    out = []
    t = start
    for p in final:
        d = duration * len(p) / total_chars
        out.append({"start": round(t, 3), "end": round(t + d, 3), "text": p})
        t += d
    return out


def build_srt(chunks: list[dict]) -> str:
    def stamp(t: float) -> str:
        ms = int(round(t * 1000))
        h, rem = divmod(ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks = [
        f"{i + 1}\n{stamp(c['start'])} --> {stamp(c['end'])}\n{c['text']}\n"
        for i, c in enumerate(chunks)
    ]
    return "\n".join(blocks)
