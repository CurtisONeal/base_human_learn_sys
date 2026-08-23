"""Reading keyframes: local OCR first, a vision model only where OCR isn't enough.

Two tiers, because they have wildly different economics:

* **OCR** is free and local. On macOS the Vision framework is fast, good on code
  and UI, and costs nothing — so it runs on every keyframe unconditionally.
* **A vision model** is metered API spend, per image, and cannot use the
  subscription backend: `claude-cli` runs with `Read` disabled (the
  prompt-injection guard for untrusted source content), so images cannot be
  handed to it without reopening exactly that hole. Vision therefore escalates
  only for frames OCR cannot explain — diagrams, charts, plots, UI layouts,
  where reading the glyphs is not the same as understanding the picture.

That ordering is the whole cost story. Reading every frame with a frontier
model runs roughly $0.70-0.80 per video; OCR-first with selective escalation
usually lands in cents, because most slides *are* mostly text.
"""

import base64
import platform
import re
from pathlib import Path
from typing import List, Optional

import litellm
from pydantic import BaseModel

from video2mdnotes.config import settings
from video2mdnotes.logger import logger
from video2mdnotes.core.frames import Keyframe
from video2mdnotes.core.summarizer import _api_key_for
from video2mdnotes.core.ranking import select_for_escalation

# A frame whose OCR yields at least this much real text is treated as
# self-explanatory. Below it, the picture is probably carrying the meaning.
_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)

VLM_PROMPT = (
    "This is a single frame from an instructional video. In 1-3 plain sentences, "
    "describe what is visible and what it conveys — the structure of any diagram, "
    "chart, table, or UI. "
    "Write flowing prose only. Do NOT use markdown headings, bold labels, or "
    "bullet lists: this text is inserted into an existing notes document and any "
    "heading you emit breaks its structure. "
    "Do not speculate about parts of the video you cannot see, and do not invent "
    "labels or values that are not legible in the image. If the frame carries no "
    "meaningful information, reply exactly: NO CONTENT."
)

# Belt and braces: a model that emits a heading anyway must not be able to
# corrupt the document's structure.
_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)


class FrameReading(BaseModel):
    """What was recovered from one keyframe, and how."""
    timestamp: float
    label: str
    image_path: Path
    ocr_text: str = ""
    description: str = ""
    # "ocr" | "ocr+vlm:<model>" | "none" — provenance, same discipline as
    # transcript_source: a machine reading and a model's interpretation are
    # different kinds of evidence and must stay distinguishable.
    source: str = "none"

    @property
    def is_empty(self) -> bool:
        return not (self.ocr_text.strip() or self.description.strip())


def ocr_available() -> bool:
    """True when local OCR can run on this machine."""
    if settings.ocr_backend == "none":
        return False
    if platform.system() != "Darwin":
        return False
    try:
        import ocrmac  # noqa: F401
        return True
    except ImportError:
        return False


def ocr_image(image_path: Path) -> str:
    """Read text from an image using the macOS Vision framework.

    Returns "" on any failure — a frame we cannot read is not an error, it just
    escalates to the vision tier (or is dropped).
    """
    try:
        from ocrmac import ocrmac as _ocrmac
        annotations = _ocrmac.OCR(str(image_path)).recognize()
    except Exception as e:  # noqa: BLE001 - OCR is best-effort by design
        logger.warning(f"OCR failed on {image_path.name}: {e}")
        return ""

    lines = []
    for annotation in annotations or []:
        # ocrmac yields (text, confidence, bbox)
        text = annotation[0] if isinstance(annotation, (tuple, list)) else str(annotation)
        confidence = annotation[1] if isinstance(annotation, (tuple, list)) and len(annotation) > 1 else 1.0
        if text and confidence >= settings.ocr_min_confidence:
            lines.append(str(text).strip())
    return "\n".join(line for line in lines if line)


def looks_like_burned_in_subtitles(ocr_text: str, transcript_text: str) -> bool:
    """True when a frame's OCR is just the spoken words rendered into the video.

    Shorts and social clips burn subtitles into the pixels because they autoplay
    muted. That breaks two things at once: the domain/visual probe reads OCR
    yield, so a wordless demonstration looks text-rich (measured — an Aikido
    Short classified as 'animated' on the strength of its own subtitles), and
    the notes end up carrying the same sentence twice, once from the transcript
    and once as "on screen".

    The discriminator is free because both strings are already in memory: high
    overlap with the transcript means subtitles, low overlap means real
    on-screen content worth keeping.
    """
    words = set(_WORD.findall((ocr_text or "").lower()))
    if len(words) < settings.subtitle_min_words:
        return False
    spoken = set(_WORD.findall((transcript_text or "").lower()))
    if not spoken:
        return False
    return len(words & spoken) / len(words) >= settings.subtitle_overlap


def is_low_information(image_path: Path) -> bool:
    """True when a frame is too featureless to be worth describing.

    Transition and flash frames are near-uniform: a blown-out white field, a
    fade to black. Sending one to a vision model does not yield "NO CONTENT" —
    measured on an Aikido demonstration, a washed-out frame was confidently
    described as "white athletic shoes or sneakers arranged on a white
    surface". There are no shoes. The model confabulates from noise rather than
    declining, so the frame has to be filtered before the call, not after.

    Free to compute, and it runs before the only per-frame cost in the pipeline.
    """
    try:
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(image_path) as image:
            small = image.convert("L").resize((64, 64))
            pixels = list(small.getdata())
    except Exception:  # noqa: BLE001 - an unreadable frame is handled elsewhere
        return False

    if not pixels:
        return True

    # Contrast, not brightness. A featureless frame has little variation
    # regardless of whether it is a blown-out white flash or a fade to black,
    # so standard deviation separates them where a brightness threshold would
    # catch only one. Measured on the Aikido demonstration: the frame that was
    # confabulated into "white athletic shoes" scored 21.9, while every frame
    # carrying real content scored 38.2 or above — a clean margin either side
    # of the default.
    mean = sum(pixels) / len(pixels)
    variance = sum((v - mean) ** 2 for v in pixels) / len(pixels)
    return (variance ** 0.5) < settings.frame_blank_min_contrast


def needs_vision_model(ocr_text: str) -> bool:
    """True when OCR alone probably didn't capture what the frame conveys.

    A slide that OCR'd into a paragraph of prose is already understood. A frame
    that yields a handful of axis labels is a chart, and the chart is the point.
    """
    return len(_WORD.findall(ocr_text)) < settings.vlm_escalate_below_words


def _encode(image_path: Path) -> tuple[str, str]:
    media_type = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    return media_type, base64.standard_b64encode(image_path.read_bytes()).decode("utf-8")


def describe_image(image_path: Path, model: Optional[str] = None) -> str:
    """Ask a vision model what a frame shows. Returns "" if it declines or fails."""
    model = model or settings.vlm_model
    media_type, data = _encode(image_path)

    try:
        response = litellm.completion(
            model=model,
            # None lets litellm fall back to the provider's env var, which is
            # the usual setup when the repo .env still holds a placeholder.
            api_key=_api_key_for(model),
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:{media_type};base64,{data}"}},
                    {"type": "text", "text": VLM_PROMPT},
                ],
            }],
            timeout=180,
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as e:  # noqa: BLE001 - visual enrichment is never fatal
        logger.warning(f"Vision model failed on {image_path.name}: {e}")
        return ""

    if text.upper().startswith("NO CONTENT"):
        return ""
    # Demote any heading the model emitted despite the instruction — an H1
    # inside a ### frame block silently breaks the notes' heading hierarchy.
    return _HEADING.sub("", text).strip()


def read_frames(
    frames: List[Keyframe],
    segments=None,
    video_duration: Optional[float] = None,
) -> List[FrameReading]:
    """Run the OCR-then-escalate tier over extracted keyframes.

    OCR runs on everything (it is free). The paid vision tier runs only on
    frames the gate marks eligible AND the ranker picks within budget —
    `segments` and `video_duration` feed that ranking and are optional.
    """
    readings: List[FrameReading] = []
    have_ocr = ocr_available()
    if not have_ocr and settings.ocr_backend != "none":
        logger.warning(
            "Local OCR unavailable (macOS Vision only) — every frame will "
            "escalate to the vision model, which costs metered API spend. "
            "Set OCR_BACKEND=none or EXTRACT_FRAMES=false to avoid this."
        )

    # Pass 1: OCR everything (free) and apply the eligibility gate.
    ocr_texts = [ocr_image(f.path) if have_ocr else "" for f in frames]

    # Discard OCR that is merely the burned-in subtitles of what was said. Done
    # before the probe reads these yields, since otherwise a wordless clip
    # classifies on the strength of its own captions.
    spoken = " ".join((seg.text or "") for seg in (segments or []))
    if spoken:
        subtitle_hits = 0
        for i, text in enumerate(ocr_texts):
            if looks_like_burned_in_subtitles(text, spoken):
                ocr_texts[i] = ""
                subtitle_hits += 1
        if subtitle_hits:
            logger.info(
                f"Discarded burned-in subtitles on {subtitle_hits} frame(s) — "
                f"they duplicate the transcript and skew content classification."
            )
    eligible = [
        settings.vlm_enabled and needs_vision_model(text) for text in ocr_texts
    ]

    # Probe: the OCR above is already paid for (it is free), so use it to infer
    # the content shape before deciding what to spend. Only applied when no
    # explicit profile or source-map hit already settled it.
    if settings.profile_probe and not settings.__dict__.get("_profile_locked"):
        from video2mdnotes.core import profiles as _profiles
        guess = _profiles.classify_by_ocr(ocr_texts)
        if guess:
            picked = _profiles.PROFILES[guess]
            logger.info(f"Profile '{guess}' (probe, from OCR yield): {picked.why}")
            _profiles.apply_paid_only(picked)
            eligible = [
                settings.vlm_enabled and needs_vision_model(t) for t in ocr_texts
            ]

    # Pass 2: rank the eligible frames and spend the budget on the best of
    # them, rather than on whichever happened to come first.
    chosen = select_for_escalation(
        frames, eligible, settings.vlm_max_frames, segments, video_duration
    )
    if sum(eligible) > len(chosen):
        logger.info(
            f"{sum(eligible)} frame(s) eligible for the vision model, budget is "
            f"{settings.vlm_max_frames} — ranking by dwell time, uniqueness and "
            f"audio cues."
        )

    escalated = 0
    for frame, text in zip(frames, ocr_texts):
        reading = FrameReading(
            timestamp=frame.timestamp,
            label=frame.label,
            image_path=frame.path,
            ocr_text=text,
            source="ocr" if text else "none",
        )

        if frame.path in chosen:
            if is_low_information(frame.path):
                logger.info(
                    f"Skipping near-blank frame at {frame.label} — a vision "
                    f"model confabulates on featureless input rather than "
                    f"declining."
                )
                readings.append(reading)
                continue
            description = describe_image(frame.path)
            if description:
                reading.description = description
                reading.source = f"{reading.source}+vlm:{settings.vlm_model}".lstrip("+")
                escalated += 1

        readings.append(reading)

    kept = [r for r in readings if not r.is_empty]
    logger.success(
        f"Read {len(kept)}/{len(frames)} keyframe(s) "
        f"({escalated} escalated to {settings.vlm_model})"
    )
    return kept


def narration_at(segments, timestamp: float, window: float) -> str:
    """Transcript text spoken around a given moment.

    This is what makes audio and visuals usable together. The summary appends
    the transcript as raw text with no timestamps, so without this a reader has
    no way to connect a frame at [3m40s] to the words that accompanied it —
    exactly the correlation a "click here, then here" tutorial depends on.
    """
    if not segments:
        return ""
    parts = [
        (seg.text or "").strip()
        for seg in segments
        if seg.start <= timestamp + window and seg.end >= timestamp - window
    ]
    return " ".join(p for p in parts if p).strip()


def render_markdown(
    readings: List[FrameReading],
    frames_dirname: str = "frames",
    segments=None,
) -> str:
    """Render frame readings as a markdown section with embedded images.

    Image links are relative so the notes directory stays portable — moving or
    zipping the run directory keeps the images resolving.
    """
    if not readings:
        return ""

    lines = ["## Visual Content\n"]
    lines.append(
        "_Extracted from video keyframes. OCR text is a machine reading of what "
        "is on screen; descriptions are a vision model's interpretation._\n"
    )
    for reading in readings:
        lines.append(f"### [{reading.label}]\n")
        lines.append(f"![Frame at {reading.label}]({frames_dirname}/{reading.image_path.name})\n")
        if reading.ocr_text.strip():
            lines.append("**On screen:**\n")
            lines.append("```text")
            lines.append(reading.ocr_text.strip())
            lines.append("```\n")
        if reading.description.strip():
            lines.append(f"**Description:** {reading.description.strip()}\n")
        if segments:
            said = narration_at(segments, reading.timestamp, settings.visual_narration_window)
            if said:
                lines.append(f"**Narration:** {said}\n")
        lines.append(f"_source: {reading.source}_\n")
    return "\n".join(lines)


# The summarizer appends the raw transcript under this heading. Visual content
# belongs before it, so the notes read summary -> visuals -> transcript.
TRANSCRIPT_MARKER = "\n\n## Transcript\n"
VISUAL_HEADING = "## Visual Content"


def insert_visual_section(summary_text: str, visual_markdown: str) -> str:
    """Splice a visual section into summary text at the right place.

    Shared by the inline pipeline and the visuals-only pass so both produce
    byte-identical placement — the insertion point is the one thing a separate
    pass could plausibly get wrong, and having two implementations of it is how
    they would drift.

    Replaces an existing visual section rather than appending a second one, so
    re-running the pass is idempotent.
    """
    if not visual_markdown.strip():
        return summary_text

    # Drop any previous visual section, wherever it sits.
    if VISUAL_HEADING in summary_text:
        head, _, rest = summary_text.partition(VISUAL_HEADING)
        # The old section runs until the transcript marker, or to the end.
        if TRANSCRIPT_MARKER in rest:
            _, _, tail = rest.partition(TRANSCRIPT_MARKER)
            summary_text = f"{head.rstrip()}{TRANSCRIPT_MARKER}{tail}"
        else:
            summary_text = head.rstrip()

    if TRANSCRIPT_MARKER in summary_text:
        head, _, tail = summary_text.partition(TRANSCRIPT_MARKER)
        return f"{head.rstrip()}\n\n{visual_markdown}{TRANSCRIPT_MARKER}{tail}"
    return f"{summary_text.rstrip()}\n\n{visual_markdown}"


def has_usable_notes(summary_text: str) -> bool:
    """False when a run produced nothing worth reading.

    The empty-transcript placeholder is a legitimate outcome (a music-only
    video genuinely has no speech) but it is not notes, and in a directory
    listing it is indistinguishable from a real result.
    """
    from video2mdnotes.core.summarizer import EMPTY_TRANSCRIPT_PLACEHOLDER

    body = summary_text
    if TRANSCRIPT_MARKER in body:
        body, _, _ = body.partition(TRANSCRIPT_MARKER)
    body = body.replace(EMPTY_TRANSCRIPT_PLACEHOLDER, "").strip()
    # A surviving visual section means the frames carried the content even
    # though the audio did not.
    return bool(body)
