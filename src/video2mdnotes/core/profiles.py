"""Content-shape profiles for visual extraction.

The right settings for reading a video's screen depend on what kind of video it
is, and the differences are not marginal. Measured on real content:

    animated explainer (3Blue1Brown)   24 frames   ~3 OCR words   100% escalate
    lecture w/ slides (Karpathy)       17 frames    35 OCR words    29% escalate
    software walkthrough (Postman)      8 frames    82 OCR words    37% escalate

Three genuinely different shapes. A single global threshold serves one of them
and mis-serves the rest: 0.3 (tuned for hard slide cuts) yielded *two* frames
from a 19-minute animated explainer, while the Postman screencast over-fires
scene detection on cursor movement and needs aggressive de-duplication instead.

Selection is deliberately ordered cheapest-and-most-certain first:

    explicit --profile  >  source map  >  probe  >  default

The **probe** is the interesting one, and it reuses the pipeline's central
trick: OCR is free, so use it to decide how to spend money. Sample a handful of
already-extracted frames, read them, and let the OCR yield say what kind of
video this is — dense text means a UI or slide deck, sparse text means the
picture is carrying the meaning.

Deliberate scope limit: a profile never changes *extraction* thresholds when it
came from the probe, because the probe needs extracted frames to classify and
re-extracting to apply its own answer would double the work for a guess. Probe
results tune only the paid decisions (escalation, budget, model). Extraction
thresholds move only on an explicit --profile or a source-map hit, where the
answer is known before any frame is pulled.
"""

import re
from typing import Dict, List, Optional

from pydantic import BaseModel

from video2mdnotes.config import settings
from video2mdnotes.logger import logger


class Profile(BaseModel):
    """Settings bundle for one content shape."""
    name: str
    why: str
    scene_threshold: float
    dedupe_threshold: int
    escalate_below_words: int
    max_frames: int
    vlm_max_frames: int
    vlm_model: Optional[str] = None   # None = leave VLM_MODEL as configured
    extract_frames: bool = True


# Only `animated`, `lecture` and `screencast` carry measured numbers; the others
# are reasoned starting points and say so, so nobody mistakes assertion for
# evidence.
PROFILES: Dict[str, Profile] = {
    "animated": Profile(
        name="animated",
        why="measured: 3Blue1Brown, 24 frames, ~3 OCR words, 100% escalated",
        # Continuous animation has no hard cuts; 0.3 found 2 frames in 19 min.
        scene_threshold=0.15,
        dedupe_threshold=6,
        # Diagrams are the content and OCR yields fragments, so escalate freely.
        escalate_below_words=16,
        max_frames=40,
        vlm_max_frames=15,
    ),
    "lecture": Profile(
        name="lecture",
        why="measured: Karpathy, 17 frames, 35 OCR words, 29% escalated",
        scene_threshold=0.15,
        dedupe_threshold=6,
        escalate_below_words=12,
        max_frames=40,
        vlm_max_frames=12,
    ),
    "screencast": Profile(
        name="screencast",
        why="measured: Postman, 8 frames, 82 OCR words, 37% escalated",
        scene_threshold=0.15,
        # Cursor movement and typing over-fire scene detection on a UI, so merge
        # harder than elsewhere.
        dedupe_threshold=10,
        # The UI is dense text and OCR reads it well, so escalate reluctantly.
        escalate_below_words=8,
        max_frames=30,
        vlm_max_frames=10,
    ),
    "slides": Profile(
        name="slides",
        why="reasoned, not measured: hard cuts, prose slides OCR reads well",
        scene_threshold=0.3,
        dedupe_threshold=6,
        escalate_below_words=8,
        max_frames=40,
        vlm_max_frames=10,
    ),
    "talking_head": Profile(
        name="talking_head",
        why="reasoned, not measured: audio is the content; frames are a face",
        scene_threshold=0.3,
        dedupe_threshold=6,
        escalate_below_words=0,     # never escalate
        max_frames=10,
        vlm_max_frames=0,
        extract_frames=False,       # do not spend anything on visuals
    ),
}


def parse_source_map(raw: str, valid: Optional[Dict] = None) -> List[tuple]:
    """Parse "substring=profile,substring=profile" into ordered pairs.

    Ordered rather than a dict so the first match wins predictably, and
    extensible from .env without touching code — which is what lets Udemy,
    DataCamp and the rest be added later as they get characterised.
    """
    pairs = []
    for chunk in (raw or "").split(","):
        if "=" in chunk:
            needle, _, name = chunk.partition("=")
            needle, name = needle.strip().lower(), name.strip().lower()
            if needle and name in (PROFILES if valid is None else valid):
                pairs.append((needle, name))
    return pairs


def from_source_map(source) -> Optional[Profile]:
    """Match a known channel/uploader/URL to a profile. Free and instant."""
    haystack = " ".join(
        str(getattr(source, f, "") or "")
        for f in ("uploader", "channel", "url", "playlist_title", "title")
    ).lower()
    for needle, name in parse_source_map(settings.profile_source_map):
        if needle in haystack:
            return PROFILES[name]
    return None


_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


def classify_by_ocr(ocr_samples: List[str]) -> Optional[str]:
    """Infer content shape from OCR yield across a few sampled frames.

    This is the same move captions-first makes: read the free signal to decide
    whether to spend. Dense text means a UI or slide deck that OCR will largely
    handle by itself; sparse text means the picture carries the meaning and the
    vision model is worth paying for.

    Returns None when the sample is too small to say anything — an unconfident
    guess here silently changes what gets paid for.
    """
    if len(ocr_samples) < settings.profile_probe_min_samples:
        return None

    counts = sorted(len(_WORD.findall(text)) for text in ocr_samples)
    median = counts[len(counts) // 2]

    if median >= settings.profile_probe_dense_words:
        return "screencast"
    if median >= settings.profile_probe_sparse_words:
        return "lecture"
    if median == 0 and all(c == 0 for c in counts):
        # Nothing readable anywhere: either a face or pure imagery. Prefer the
        # cheap reading — talking_head spends nothing — since guessing
        # "animated" here would escalate every frame on a hunch.
        return "talking_head"
    return "animated"


def resolve(source, explicit: Optional[str] = None) -> Optional[Profile]:
    """Pick a profile by precedence: explicit > source map > (probe, later)."""
    if explicit:
        name = explicit.strip().lower()
        if name not in PROFILES:
            raise ValueError(
                f"Unknown profile {explicit!r}. Known: {', '.join(sorted(PROFILES))}"
            )
        logger.info(f"Profile '{name}' (explicit): {PROFILES[name].why}")
        return PROFILES[name]

    mapped = from_source_map(source)
    if mapped:
        logger.info(f"Profile '{mapped.name}' (source map): {mapped.why}")
        return mapped
    return None


def apply(profile: Profile) -> None:
    """Push a profile's values into settings for this run.

    Mutating settings keeps every existing call site unchanged — the profile is
    a way of choosing values, not a second configuration system to thread
    through the pipeline.
    """
    settings.frame_scene_threshold = profile.scene_threshold
    settings.frame_dedupe_threshold = profile.dedupe_threshold
    settings.vlm_escalate_below_words = profile.escalate_below_words
    settings.frame_max = profile.max_frames
    settings.vlm_max_frames = profile.vlm_max_frames
    if profile.vlm_model:
        settings.vlm_model = profile.vlm_model
    if not profile.extract_frames:
        settings.extract_frames = False


def apply_paid_only(profile: Profile) -> None:
    """Apply only what an inferred content shape legitimately decides.

    Two deliberate limits, both about not overreaching on a guess:

    * **Extraction thresholds are untouched.** The frames are already extracted
      by the time the probe can classify them, so acting on its answer would
      mean re-extracting for a guess.
    * **VLM_MAX_FRAMES is untouched.** That is the operator's spending cap, not
      a property of the content. The probe decides what *qualifies* for the
      vision model; how much you are willing to spend stays yours. A profile
      that wants no spending expresses it with escalate_below_words=0, which
      makes nothing eligible, rather than by rewriting the budget.
    """
    settings.vlm_escalate_below_words = profile.escalate_below_words
    if profile.vlm_model:
        settings.vlm_model = profile.vlm_model
