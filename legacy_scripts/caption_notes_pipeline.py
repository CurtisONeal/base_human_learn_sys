from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import litellm

from video2mdnotes.config import settings
from video2mdnotes.core.downloader import sanitize_filename


@dataclass(frozen=True)
class CaptionSegment:
    start: float
    end: float
    text: str


def format_timestamp(seconds: float) -> str:
    whole = int(seconds)
    ms = int((seconds - whole) * 1000)
    return f"{str(dt.timedelta(seconds=whole))}.{ms:03d}"


def load_caption_segments(path: Path) -> list[CaptionSegment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments: list[CaptionSegment] = []

    for event in data.get("events", []):
        text = "".join(seg.get("utf8", "") for seg in event.get("segs", []))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue

        start_ms = int(event.get("tStartMs", 0))
        duration_ms = int(event.get("dDurationMs", 0))
        segments.append(
            CaptionSegment(
                start=start_ms / 1000,
                end=(start_ms + duration_ms) / 1000,
                text=text,
            )
        )

    return segments


def write_transcript(
    segments: list[CaptionSegment],
    transcript_path: Path,
    title: str,
    source_url: str,
    captured_at: str,
) -> None:
    lines = [
        "---",
        f'title: "{title}"',
        f"source: {source_url}",
        "model: youtube-auto-captions",
        "language: en-orig",
        f"generated_at: {captured_at}",
        "---",
        "",
        f"# {title}",
        "",
        f"> Source: {source_url}",
        f"> Captured: {captured_at}",
        "> Transcript source: YouTube automatic captions",
        "",
        "## Transcript",
        "",
    ]

    for segment in segments:
        lines.append(f"- [{format_timestamp(segment.start)}] {segment.text}")

    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def chunk_segments(
    segments: list[CaptionSegment],
    max_words: int,
) -> list[list[CaptionSegment]]:
    chunks: list[list[CaptionSegment]] = []
    current: list[CaptionSegment] = []
    current_words = 0

    for segment in segments:
        words = len(segment.text.split())
        if current and current_words + words > max_words:
            chunks.append(current)
            current = []
            current_words = 0
        current.append(segment)
        current_words += words

    if current:
        chunks.append(current)

    return chunks


def chunk_to_markdown(chunk: list[CaptionSegment], index: int, total: int) -> str:
    start = format_timestamp(chunk[0].start)
    end = format_timestamp(chunk[-1].end)
    lines = [f"# Chunk {index}/{total} ({start} - {end})", ""]
    for segment in chunk:
        lines.append(f"- [{format_timestamp(segment.start)}] {segment.text}")
    return "\n".join(lines)


def completion(messages: list[dict[str, str]], max_tokens: int) -> str:
    litellm.api_key = settings.openai_api_key
    litellm.anthropic_api_key = settings.anthropic_api_key
    response = litellm.completion(
        model=settings.llm_model,
        messages=messages,
        max_tokens=max_tokens,
        timeout=600,
    )
    content = response.choices[0].message.content
    if not content:
        raise RuntimeError("LLM returned empty content")
    return content


def summarize_chunk(
    title: str,
    source_url: str,
    chunk_md: str,
    index: int,
    total: int,
) -> str:
    system = (
        "You create source-grounded study notes from transcript chunks. "
        "Preserve technical claims, named concepts, commands, product names, "
        "exam guidance, caveats, examples, and timestamps. Do not invent facts. "
        "This is an intermediate chunk summary for later synthesis."
    )
    user = f"""
Video title: {title}
Source URL: {source_url}
Chunk: {index} of {total}

Summarize this transcript chunk for later synthesis. Include:
- Time range and 8-16 high-signal bullets with timestamps.
- Key concepts and vocabulary.
- Commands, UI labels, APIs, tools, or configuration mentioned.
- Exam-relevant points.
- Practice ideas or implementation moves.
- Open questions or uncertainty caused by caption quality.

Transcript chunk:

{chunk_md}
"""
    return completion(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=2200,
    )


def synthesize_final_notes(
    title: str,
    source_url: str,
    transcript_path: Path,
    captured_at: str,
    chunk_summaries: list[str],
) -> str:
    summaries = "\n\n---\n\n".join(chunk_summaries)
    user = f"""
Video title: {title}
Source URL: {source_url}
Captured: {captured_at}
Transcript file: {transcript_path}
Transcript source: YouTube automatic captions.
Scope: Full video, reconstructed from all chunk summaries.

Create final Markdown learning notes for a working software/AI engineer studying
Claude Certified Architect Foundations material. Use this exact section order:
YAML frontmatter; title and source block; shelf overview; purpose; learning
objectives; learning science lens; essential new ideas and framing concepts;
key concepts and vocabulary; lesson type variant; fast summary; concept map
decision; concept map; core notes; UI elements and interaction steps; examples
and non-examples; worked example and practice ladder; hands-on lab; teach-back
plan; visuals and diagrams; neurodiversity and accessibility supports;
misconceptions and edge cases; active recall; spaced review; transfer to my
work; code, commands, syntax, and templates; confidence and inference; source
traceability; next actions.

Constraints:
- Use only the chunk summaries below and clearly mark inference.
- Do not append the full transcript.
- Keep claims traceable to timestamps where available.
- Include a Mermaid concept map because the course connects agent architecture,
  tools, workflows, memory, retrieval, evaluation, and deployment concepts.
- Make tables valid GitHub-Flavored Markdown.
- Include at least 10 active-recall questions and a three-stage spaced review.

Chunk summaries:

{summaries}
"""
    return completion(
        [
            {
                "role": "system",
                "content": (
                    "You write source-grounded Markdown study notes with adult-learning "
                    "structure, retrieval practice, spaced review, transfer prompts, "
                    "implementation scaffolding, and clear confidence labels."
                ),
            },
            {"role": "user", "content": user},
        ],
        max_tokens=9000,
    )


def default_output_dir(title: str) -> Path:
    safe_title = sanitize_filename(title)
    return settings.output_dir / f"{dt.date.today():%Y%m%d}_{safe_title}_captions"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--caption-json", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--output-dir")
    parser.add_argument("--captured-at")
    parser.add_argument("--max-words", type=int, default=7500)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()

    caption_path = Path(args.caption_json)
    captured_at = args.captured_at or dt.date.today().isoformat()
    safe_title = sanitize_filename(args.title)
    project_dir = Path(args.output_dir) if args.output_dir else default_output_dir(args.title)
    transcripts_dir = project_dir / "transcripts"
    summaries_dir = project_dir / "summaries"
    chunks_dir = project_dir / "chunk_summaries"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    summaries_dir.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)

    segments = load_caption_segments(caption_path)
    if not segments:
        raise RuntimeError(f"No caption text found in {caption_path}")

    transcript_path = transcripts_dir / f"{safe_title}.captions.md"
    summary_path = summaries_dir / f"{safe_title}.summary.md"
    write_transcript(segments, transcript_path, args.title, args.url, captured_at)
    (project_dir / "original_url.txt").write_text(args.url, encoding="utf-8")
    (project_dir / "caption_source.txt").write_text(str(caption_path), encoding="utf-8")

    chunks = chunk_segments(segments, args.max_words)
    total_words = sum(len(segment.text.split()) for segment in segments)
    print(f"output_dir={project_dir}")
    print(f"segments={len(segments)} words={total_words} chunks={len(chunks)}")
    print(f"transcript={transcript_path}")

    if args.prepare_only:
        return 0

    chunk_summaries: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_path = chunks_dir / f"chunk_{index:02d}_of_{len(chunks):02d}.md"
        if chunk_path.exists():
            text = chunk_path.read_text(encoding="utf-8")
            print(f"chunk {index}/{len(chunks)} exists")
        else:
            print(f"summarizing chunk {index}/{len(chunks)}")
            text = summarize_chunk(
                title=args.title,
                source_url=args.url,
                chunk_md=chunk_to_markdown(chunk, index, len(chunks)),
                index=index,
                total=len(chunks),
            )
            chunk_path.write_text(text + "\n", encoding="utf-8")
        chunk_summaries.append(text)

    print("synthesizing final notes")
    final_notes = synthesize_final_notes(
        title=args.title,
        source_url=args.url,
        transcript_path=transcript_path,
        captured_at=captured_at,
        chunk_summaries=chunk_summaries,
    )
    summary_path.write_text(final_notes + "\n", encoding="utf-8")
    print(f"summary={summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
