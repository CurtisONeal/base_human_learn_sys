"""Choose the transcription model from free signals, before paying for CPU.

Two models are kept installed, because neither wins everywhere:

    large-v3          multilingual · 3.1 GB · slow · the DEFAULT
    distil-large-v3   ENGLISH ONLY · 1.5 GB · fast · far better on noisy audio

Measured on a 14-minute far-field recording (people across a room):

    medium            63s   318 words    0 repeated 5-grams
    large-v3         352s   473 words   10 repeated 5-grams   <- looping
    distil-large-v3   78s   635 words    0 repeated 5-grams   <- best

So distil wins on hard audio — but it cannot transcribe Japanese at all, and a
corpus with Aikido material in it will contain Japanese terminology. Silently
running an English-only model over Japanese produces confident phonetic
nonsense, which is worse than a slow correct answer.

Hence the ordering, which is a policy not an optimisation:

    1. Non-English context detected  ->  large-v3.  Always. Never overridden.
    2. Otherwise, audio measures noisy  ->  distil-large-v3.
    3. Otherwise  ->  large-v3 (default; assume quiet).

Both signals are free. Metadata is already in hand; the audio measurement is
one ffmpeg pass with no decoding of speech.
"""

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

MULTILINGUAL = "large-v3"
ENGLISH_NOISY = "distil-large-v3"

# Terms that mean "this material is not purely English". Aikido vocabulary is
# listed explicitly because it is the known case in this corpus and because a
# romanised Japanese term ("shomenuchi", "ikkyo") carries no diacritics for a
# language detector to notice.
_NON_ENGLISH_CONTEXT = re.compile(
    r"\b("
    r"aikido|aikikai|usaf|dojo|sensei|shihan|kyu|dan\b|uke|nage|tori|"
    r"shomenuchi|yokomenuchi|katatedori|tsuki|ikkyo|nikyo|sankyo|yonkyo|gokyo|"
    r"iriminage|kotegaeshi|shihonage|kaitenage|tenchinage|koshinage|"
    r"randori|ukemi|hanmi|tenkan|irimi|zanshin|kiai|keiko|embu|kata\b|"
    r"judo|karate|kendo|iaido|jujutsu|jiu.?jitsu|budo|bushido|"
    r"japanese|nihongo|kanji|hiragana|katakana|"
    r"chinese|mandarin|spanish|french|german|korean|portuguese|italian"
    r")\b",
    re.IGNORECASE,
)

# Mean programme level below this reads as far-field or noisy. Calibrated on a
# real far-field recording at -33.0 dB versus close-mic speech nearer -20 dB.
#
# HONEST LIMIT: this threshold rests on ONE measured noisy sample. It is a
# starting point, not a tuned value, and it is a config knob for that reason.
NOISY_MEAN_DB = -28.0


@dataclass
class ModelChoice:
    model: str
    reason: str
    mean_db: Optional[float] = None
    non_english_hit: Optional[str] = None

    @property
    def is_english_only(self) -> bool:
        return self.model == ENGLISH_NOISY


def detect_non_english_context(text: str) -> Optional[str]:
    """Return the term that suggests non-English content, or None.

    Matching vocabulary rather than running a language detector: the metadata
    is usually an English sentence *about* Japanese material ("2nd kyu test,
    shomenuchi ikkyo"), which every language detector calls English while the
    audio is full of Japanese terms.
    """
    match = _NON_ENGLISH_CONTEXT.search(text or "")
    return match.group(0) if match else None


def measure_mean_db(audio_path: Path) -> Optional[float]:
    """Mean programme level in dBFS, via one ffmpeg pass. No speech decoding."""
    binary = shutil.which("ffmpeg")
    if not binary or not Path(audio_path).exists():
        return None
    try:
        proc = subprocess.run(
            [binary, "-hide_banner", "-i", str(audio_path),
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", proc.stderr or "")
    return float(match.group(1)) if match else None


def choose_model(
    metadata_text: str = "",
    audio_path: Optional[Path] = None,
    noisy_threshold_db: float = NOISY_MEAN_DB,
) -> ModelChoice:
    """Pick a Whisper model from metadata and a cheap audio measurement.

    Language context is checked FIRST and is never overridden by noise. A noisy
    Aikido recording still goes to the multilingual model: getting the Japanese
    terms approximately right matters more than recovering extra English words
    around them, and the English-only model cannot do the former at all.
    """
    hit = detect_non_english_context(metadata_text)
    if hit:
        return ModelChoice(
            model=MULTILINGUAL,
            reason=f"non-English context ({hit!r}) — multilingual required, "
                   "noise is not allowed to override this",
            non_english_hit=hit,
        )

    mean_db = measure_mean_db(audio_path) if audio_path else None
    if mean_db is not None and mean_db < noisy_threshold_db:
        return ModelChoice(
            model=ENGLISH_NOISY,
            reason=f"quiet/far-field audio (mean {mean_db:.1f} dB < "
                   f"{noisy_threshold_db} dB) and no non-English signal",
            mean_db=mean_db,
        )

    return ModelChoice(
        model=MULTILINGUAL,
        reason=("default: multilingual, assuming close-mic speech"
                + (f" (mean {mean_db:.1f} dB)" if mean_db is not None else "")),
        mean_db=mean_db,
    )
