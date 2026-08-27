import datetime as dt
import re
from collections import Counter
from pathlib import Path
from typing import List
from pydantic import BaseModel
from faster_whisper import WhisperModel
from rich.progress import Progress, SpinnerColumn, TextColumn

from video2mdnotes.config import settings

# Small inline stopword list to avoid pulling in a dependency like nltk just
# for filtering common English words out of video descriptions.
_STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "your", "with", "this",
    "that", "have", "from", "will", "can", "all", "our", "about", "what",
    "how", "when", "where", "why", "who", "which", "there", "here", "just",
    "into", "out", "get", "got", "has", "had", "was", "were", "been", "being",
    "they", "them", "their", "then", "than", "some", "more", "most", "other",
    "such", "only", "own", "same", "over", "under", "again", "once", "also",
    "these", "those", "each", "very", "www", "com", "http", "https", "video",
    "channel", "subscribe", "like", "please", "new", "one", "now", "today",
    "make", "made", "use", "used", "using", "want", "need", "know", "see",
    "well", "much", "many", "any", "because", "before", "after", "while",
    "does", "did", "doing", "would", "could", "should", "might", "must",
    "let", "lets", "its", "it's", "i'm", "we're", "don't", "we", "you're",
}

class Segment(BaseModel):
    """Represents a single segment of transcribed text."""
    start: float
    end: float
    text: str

class TranscriptResult(BaseModel):
    """Represents the full transcription result."""
    source_file: Path
    language: str
    segments: List[Segment]
    full_text: str
    markdown_content: str
    model_name: str
    generated_at: dt.datetime
    # Where the text came from. Whisper output and a human-authored caption track
    # are not interchangeable evidence, so provenance is recorded rather than
    # inferred from model_name. See core/captions.py.
    transcript_source: str = "whisper"

def format_timestamp(seconds: float) -> str:
    """Formats seconds into HH:MM:SS.mmm string."""
    s = int(seconds)
    ms = int((seconds - s) * 1000)
    return f"{str(dt.timedelta(seconds=s))}.{ms:03d}"

def generate_markdown(
    title: str,
    source_file: str,
    segments: List[Segment],
    language: str,
    model: str,
    transcript_source: str = "whisper",
) -> str:
    """Generates the Markdown formatted transcript.

    `transcript_source` records how the text was obtained ("whisper" or
    "captions (manual, <lang>)"). A reader — and anyone auditing a summary built
    on it — should not have to guess whether they are looking at ASR output or a
    human-authored transcript.
    """
    lines = []
    lines.append("---")
    lines.append(f'title: "{title}"')
    lines.append(f"source: {source_file}")
    lines.append(f"transcript_source: {transcript_source}")
    lines.append(f"model: {model}")
    lines.append(f"language: {language}")
    lines.append(f"generated_at: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append("---\n")
    lines.append("# Summary\n")
    lines.append("_(Write your 5-bullet summary here.)_\n")
    lines.append("# Notes\n")
    
    for seg in segments:
        start = format_timestamp(seg.start)
        text = seg.text.strip().replace("\n", " ")
        lines.append(f"- [{start}] {text}")
    
    lines.append("")
    return "\n".join(lines)

def build_initial_prompt(title: str, tags: List[str], description: str = "") -> str:
    """
    Builds a short vocabulary-hint prompt for faster-whisper's `initial_prompt`,
    derived from metadata yt-dlp already returns (title/tags/description) so
    proper nouns like "Claude" aren't mistranscribed for lack of any hint.

    NOTE: This only covers yt-dlp-sourced input (YouTube and anything else
    download_audio() handles). Sources with no yt-dlp metadata at all (e.g. a
    generic Udemy page) would need a separate raw-HTML-scrape fallback, which
    is intentionally not implemented here.

    Args:
        title: Video title.
        tags: Curated tags/keywords for the video, if any.
        description: Video description, used as a fallback when tags are empty.

    Returns:
        A short prompt string, or just the title if no tags/description are usable.
    """
    title = (title or "").strip()

    if tags:
        keywords = ", ".join(tags[:15])
        return f"{title}. Keywords: {keywords}."

    if description:
        words = re.findall(r"[A-Za-z][A-Za-z0-9'-]*", description)
        candidates = [w for w in words if len(w) > 3 and w.lower() not in _STOPWORDS]
        counts = Counter(candidates)
        top_words = [word for word, _ in counts.most_common(15)]
        if top_words:
            keywords = ", ".join(top_words)
            return f"{title}. Keywords: {keywords}."

    return title

def select_model(audio_path: Path, title: str = "", extra_metadata: str = "") -> tuple:
    """Which Whisper model to use, and why. Free — metadata + one ffmpeg pass.

    Returns (model_name, reason). Honours ASR_AUTO_SELECT=false and treats an
    explicitly-set FW_MODEL as a pin, because someone who names a model means it.
    """
    from video2mdnotes.core.asr_select import (
        ENGLISH_NOISY, MULTILINGUAL, choose_model,
    )

    if not getattr(settings, "asr_auto_select", True):
        return settings.fw_model, "auto-select disabled (ASR_AUTO_SELECT=false)"

    # Auto-select chooses BETWEEN large-v3 and distil-large-v3. Anything else in
    # FW_MODEL is a deliberate pin and must be honoured — "tiny" in a test, or
    # an operator who knows what they want. Overriding it made the test suite
    # load a 3.1 GB model it had explicitly opted out of: 5 minutes became 39.
    if settings.fw_model not in (MULTILINGUAL, ENGLISH_NOISY):
        return settings.fw_model, f"pinned by FW_MODEL={settings.fw_model}"

    choice = choose_model(
        f"{title} {extra_metadata}",
        audio_path,
        noisy_threshold_db=getattr(settings, "asr_noisy_mean_db", -28.0),
    )
    return choice.model, choice.reason


def transcribe_audio(audio_path: Path, title: str = "Unknown", initial_prompt: str = "") -> TranscriptResult:
    """
    Transcribes the given audio file using faster-whisper.

    Args:
        audio_path: Path to the .wav file.
        title: Title of the video (for the markdown header).
        initial_prompt: Vocabulary hint passed to Whisper, e.g. from build_initial_prompt().

    Returns:
        TranscriptResult object containing the transcript and metadata.
    """
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    # Load Model
    # We use a spinner here because loading the model can take a few seconds
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        chosen, why = select_model(audio_path, title, initial_prompt)
        if chosen != settings.fw_model:
            print(f"ASR model: {chosen} — {why}")
        progress.add_task(description=f"Loading Whisper model ({chosen})...", total=None)

        model = WhisperModel(
            chosen,
            device="cpu", # Force CPU for now as per requirements (can be config'd later)
            compute_type=settings.fw_compute
        )

    # Transcribe
    # Note: faster-whisper returns a generator, so the transcription happens as we iterate
    segments_generator, info = model.transcribe(
        str(audio_path),
        language=settings.fw_lang if settings.fw_lang != "auto" else None,
        beam_size=5,
        vad_filter=True,
        initial_prompt=initial_prompt or None
    )

    segments_list: List[Segment] = []
    full_text_parts = []

    # We can't easily use a progress bar for the *duration* unless we know the audio length beforehand.
    # For simplicity in this version, we'll just iterate.
    # In a future version, we can use `pydub` or `ffprobe` to get duration and show a % bar.
    
    print(f"Transcribing {audio_path.name}...")
    
    for segment in segments_generator:
        seg_obj = Segment(
            start=segment.start,
            end=segment.end,
            text=segment.text
        )
        segments_list.append(seg_obj)
        full_text_parts.append(segment.text)

    full_text = "".join(full_text_parts)
    
    # Generate Markdown
    md_content = generate_markdown(
        title=title,
        source_file=audio_path.name,
        segments=segments_list,
        language=info.language,
        model=chosen
    )

    return TranscriptResult(
        source_file=audio_path,
        language=info.language,
        segments=segments_list,
        full_text=full_text,
        markdown_content=md_content,
        model_name=chosen,
        generated_at=dt.datetime.now()
    )
