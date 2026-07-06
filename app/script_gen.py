"""Turn source text into speakable scripts using Claude.

Three modes:
- podcast: two-host conversational episode (HOST_A / HOST_B)
- summary: short spoken digest delivered by a single narrator
- readout: the full document adapted for listening, single narrator
"""

from typing import Literal

from anthropic import AsyncAnthropic
from pydantic import BaseModel

MODEL = "claude-opus-4-8"

# Zero-arg client: resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN,
# or an `ant auth login` profile from the environment.
client = AsyncAnthropic()


class ScriptLine(BaseModel):
    speaker: Literal["HOST_A", "HOST_B", "NARRATOR"]
    text: str


class PodcastScript(BaseModel):
    title: str
    lines: list[ScriptLine]


class Narration(BaseModel):
    title: str
    text: str


DEFAULT_HOST_A = (
    "Alex — the lead host who frames the topic, asks sharp questions, "
    "and keeps the conversation moving."
)
DEFAULT_HOST_B = (
    "Jamie — the expert voice who explains the substance clearly, with "
    "concrete examples and the occasional aside."
)
DEFAULT_READER = "a warm, clear narrator with measured pacing"

_SPOKEN_STYLE = """\
- Write in English, even if the source is in another language.
- Output only what is spoken out loud: no stage directions, no sound-effect \
cues, no markdown, no bracketed notes.
- Cover the source faithfully — don't invent facts that aren't there."""


def _audience_block(audience: str | None) -> str:
    if not audience:
        return ""
    return f"""

The audience: {audience}.
Tailor the episode to these listeners — lead with and spend the most time on \
the parts of the source they'd find most interesting or useful, pick examples \
and comparisons that land for them, and pitch the level of explanation to what \
they already know. Stay faithful to the source; this changes emphasis and \
framing, not facts."""


def _podcast_system(persona_a: str, persona_b: str, audience: str | None) -> str:
    return f"""You are a podcast script writer. You turn source material \
(articles, papers, documents) into an engaging conversational podcast episode \
between two hosts.{_audience_block(audience)}

HOST_A's personality: {persona_a}
HOST_B's personality: {persona_b}

Let each host's personality clearly shape their word choice, humor, and \
perspective — but keep the content accurate.

Guidelines:
{_SPOKEN_STYLE}
- Make it sound like natural spoken conversation: contractions, short \
sentences, back-and-forth.
- Open with a brief hook and intro of the topic; close with a short recap \
and sign-off.
- Aim for roughly a 5-8 minute episode (about 800-1300 words of dialogue) \
unless the source is very short, in which case shorter is fine.
- Each line is one host's continuous turn (one to a few sentences)."""


async def generate_podcast_script(
    source_title: str,
    source_text: str,
    persona_a: str | None = None,
    persona_b: str | None = None,
    audience: str | None = None,
) -> PodcastScript:
    response = await client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_podcast_system(
            persona_a or DEFAULT_HOST_A, persona_b or DEFAULT_HOST_B, audience
        ),
        messages=[
            {
                "role": "user",
                "content": (
                    f"Source title: {source_title}\n\n"
                    f"Source material:\n\n{source_text}\n\n"
                    "Write the podcast episode script now."
                ),
            }
        ],
        output_format=PodcastScript,
    )
    script = response.parsed_output
    if script is None or not script.lines:
        raise RuntimeError("Claude did not return a usable podcast script.")
    return script


async def generate_summary(
    source_title: str,
    source_text: str,
    persona: str | None = None,
    audience: str | None = None,
) -> PodcastScript:
    system = f"""You write spoken summaries of source material (articles, \
papers, documents) to be read aloud by a single narrator.{_audience_block(audience)}

The narrator's personality: {persona or DEFAULT_READER}. Let it shape the \
delivery and word choice, but keep the content accurate and complete.

Guidelines:
{_SPOKEN_STYLE}
- Produce a concise digest of the source: the core argument, the key points, \
and the takeaway — roughly 2-4 minutes of speech (300-600 words).
- Open with one sentence saying what is being summarized; end with the main \
takeaway."""

    response = await client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Source title: {source_title}\n\n"
                    f"Source material:\n\n{source_text}\n\n"
                    "Write the spoken summary now."
                ),
            }
        ],
        output_format=Narration,
    )
    narration = response.parsed_output
    if narration is None or not narration.text.strip():
        raise RuntimeError("Claude did not return a usable summary.")
    return _narration_to_script(narration.title, narration.text)


async def generate_readout(
    source_title: str,
    source_text: str,
    persona: str | None = None,
    audience: str | None = None,
) -> PodcastScript:
    audience_note = ""
    if audience:
        audience_note = (
            f"\n\nThe audience: {audience}.\n"
            "The body must remain a complete, faithful readout — but pitch the "
            "intro, transitions, and any brief clarifying rephrasings to these "
            "listeners, and in the intro mention what they will find most "
            "relevant in this document."
        )
    system = f"""You adapt written documents into narration scripts to be read \
aloud in full by a single narrator.{audience_note}

The narrator's personality: {persona or DEFAULT_READER}. It may color the \
brief intro and transitions, but the body must stay faithful to the source.

Guidelines:
{_SPOKEN_STYLE}
- This is a READOUT, not a summary: preserve the document's full content and \
order. Rephrase only where written artifacts don't work aloud.
- Drop things that make no sense spoken: URLs, citation markers, reference \
lists, navigation text, figure/table markup (describe a table's point in a \
sentence instead).
- Speak numbers, abbreviations, and symbols naturally.
- Turn section headings into natural spoken transitions.
- Start with one short sentence introducing what is being read; end with a \
one-line sign-off."""

    # Readouts can be long — stream with generous max_tokens.
    async with client.messages.stream(
        model=MODEL,
        max_tokens=64000,
        thinking={"type": "adaptive"},
        system=system,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Source title: {source_title}\n\n"
                    f"Source material:\n\n{source_text}\n\n"
                    "Write the full narration script now."
                ),
            }
        ],
    ) as stream:
        message = await stream.get_final_message()

    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "Document is too long for a full readout — try mode=summary, "
            "or split the document."
        )
    text = "".join(b.text for b in message.content if b.type == "text").strip()
    if not text:
        raise RuntimeError("Claude did not return a usable readout script.")
    return _narration_to_script(source_title, text)


_REVISE_SYSTEM = """You revise spoken scripts (two-host podcast dialogues or \
single-narrator scripts).

You are given the current script as numbered lines, usually the original \
source material, and revision instructions. Apply the instructions precisely:
- Only change what the instructions require, plus minimal adjacent adjustments \
needed for flow.
- Keep the same speaker conventions (HOST_A/HOST_B or NARRATOR).
- Keep everything spoken-word friendly: no markdown, no stage directions.
- Stay faithful to the source material — don't invent facts.
- Return the COMPLETE revised script (every line, not just the changed ones)."""


async def revise_script(
    script: PodcastScript,
    instructions: str,
    source_text: str | None = None,
) -> PodcastScript:
    numbered = "\n".join(
        f"{i}: [{line.speaker}] {line.text}" for i, line in enumerate(script.lines)
    )
    parts = [f"Current title: {script.title}", f"Current script:\n{numbered}"]
    if source_text:
        parts.append(f"Original source material:\n\n{source_text}")
    parts.append(f"Revision instructions:\n{instructions}")

    response = await client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_REVISE_SYSTEM,
        messages=[{"role": "user", "content": "\n\n".join(parts)}],
        output_format=PodcastScript,
    )
    revised = response.parsed_output
    if revised is None or not revised.lines:
        raise RuntimeError("Claude did not return a usable revised script.")
    return revised


class LineDelivery(BaseModel):
    line: int
    spoken: str | None = None
    rate: str | None = None
    pitch: str | None = None
    volume: str | None = None
    voice: str | None = None


class VoicePlan(BaseModel):
    changes: list[LineDelivery]


_VOICE_SYSTEM = """You direct the voice-over delivery of a narrated script \
rendered with Microsoft edge-tts neural text-to-speech voices.

You are given the transcript as numbered lines — with timestamps and each \
line's current delivery settings — plus the director's instructions, which \
usually reference transcript lines by number or by quoting them.

Return delivery changes ONLY for the lines that need to change:
- spoken: replacement text to SPEAK for that line. Use for pronunciation \
fixes (respell hard words phonetically, e.g. "Xiaomi" -> "shao-mee"), \
expanding abbreviations, or inserting commas/ellipses for pauses. The \
displayed transcript and captions keep the original text.
- rate: speaking speed as a signed percent string, e.g. "+10%" or "-15%".
- pitch: signed Hz string, e.g. "+15Hz" or "-20Hz".
- volume: signed percent string, e.g. "+20%" or "-10%".
- voice: a different edge-tts voice name for this line only (rarely needed).

Leave a field null to keep its current value. To RESET a previously set value \
back to default, use "+0%" / "+0Hz" (or for spoken, repeat the display text). \
Use moderate values — beyond ±30% rate or ±40Hz pitch sounds unnatural. If an \
instruction applies to the whole delivery ("everyone slower"), emit a change \
for every line."""


async def plan_voice(
    script: PodcastScript,
    delivery: dict[int, dict],
    instructions: str,
    cues: list[dict] | None = None,
) -> VoicePlan:
    start_by_line = {c["line"]: c["start"] for c in (cues or [])}
    rows = []
    for i, line in enumerate(script.lines):
        t = start_by_line.get(i)
        stamp = f"[{int(t // 60):02d}:{int(t % 60):02d}] " if t is not None else ""
        current = delivery.get(i)
        suffix = f"  (current delivery: {current})" if current else ""
        rows.append(f"{i}: {stamp}[{line.speaker}] {line.text}{suffix}")

    response = await client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        system=_VOICE_SYSTEM,
        messages=[
            {
                "role": "user",
                "content": (
                    "Transcript:\n" + "\n".join(rows) + "\n\n"
                    f"Director's instructions:\n{instructions}"
                ),
            }
        ],
        output_format=VoicePlan,
    )
    plan = response.parsed_output
    if plan is None:
        raise RuntimeError("Claude did not return a usable voice plan.")
    return plan


def _narration_to_script(title: str, text: str) -> PodcastScript:
    """Split narration into paragraph-sized NARRATOR lines for TTS."""
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    return PodcastScript(
        title=title,
        lines=[ScriptLine(speaker="NARRATOR", text=p) for p in paragraphs],
    )
