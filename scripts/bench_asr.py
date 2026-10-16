#!/usr/bin/env python
"""Benchmark ASR backends on ONE bounded window of a long recording.

    .venv/bin/python scripts/bench_asr.py \\
        --src /path/to/long.wav --start 0:47:36 --duration 600 \\
        --captions /path/to/transcript.captions.md \\
        --info-json /path/to/yt_info.json \\
        --backend faster-whisper:large-v3 --backend parakeet-mlx \\
        --out-json results.json --text-dir /scratch/texts

The window is extracted ONCE with ffmpeg (-ss/-t, mono 16 kHz PCM) into a temp
dir and deleted afterwards; the long source is never converted whole. Each
backend's model is loaded before the clock starts, so `wall_s` is decode time
and `load_s` is reported separately (first-run downloads land in load_s).

Reference text = caption lines whose start time is inside the window. YouTube
auto-captions are machine ASR too, so WER here is *disagreement with YouTube's
ASR*, and the glossary columns are the better accuracy signal for domain terms.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from video2mdnotes.core.asr import get_backend
from video2mdnotes.core.asr import metrics as M

FALLBACK_FFMPEG = Path("/Users/s_admin/Documents/agent_set_up/bin/ffmpeg")


def resolve_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    if FALLBACK_FFMPEG.exists():
        # parakeet-mlx and mlx-whisper shell out to `ffmpeg` by name.
        os.environ["PATH"] = f"{FALLBACK_FFMPEG.parent}{os.pathsep}{os.environ.get('PATH', '')}"
        return str(FALLBACK_FFMPEG)
    sys.exit("ffmpeg not found on PATH")


def extract_window(ffmpeg: str, src: Path, start: float, dur: float, out: Path) -> float:
    """Bounded extraction; returns the actual duration written."""
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
         "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out)],
        check=True,
    )
    with wave.open(str(out)) as w:
        return w.getnframes() / w.getframerate()


def build_prompt(info_json: Path | None, title: str | None) -> str:
    from video2mdnotes.core.transcriber import build_initial_prompt

    if info_json:
        info = json.loads(info_json.read_text(encoding="utf-8"))
        return build_initial_prompt(
            info.get("title", title or ""), info.get("tags") or [], info.get("description", "")
        )
    return build_initial_prompt(title or "", [], "")


def timestamp_stats(segments, audio_s: float) -> dict:
    if not segments:
        return {"segments": 0}
    starts = [s.start for s in segments]
    gaps = [max(0.0, b.start - a.end) for a, b in zip(segments, segments[1:])]
    lens = [s.end - s.start for s in segments]
    return {
        "segments": len(segments),
        "first_start_s": round(starts[0], 2),
        "last_end_s": round(segments[-1].end, 2),
        "monotonic": all(b >= a for a, b in zip(starts, starts[1:])),
        "max_gap_s": round(max(gaps), 2) if gaps else 0.0,
        "median_seg_s": round(statistics.median(lens), 2),
        "beyond_window": sum(1 for s in segments if s.end > audio_s + 0.5),
    }


def run_one(spec: str, wav: Path, audio_s: float, prompt: str, language: str,
            ref_text: str, ref_timed, text_dir: Path | None, tag: str) -> dict:
    backend = get_backend(spec)
    row: dict = {"backend": backend.name, "model": backend.model, "spec": spec}
    ok, why = backend.available()
    row["available"], row["availability"] = ok, why
    if not ok:
        return row
    try:
        t0 = time.perf_counter()
        backend.load()
        row["load_s"] = round(time.perf_counter() - t0, 1)
        used_prompt = prompt if backend.supports_prompt else None
        row["prompt_used"] = bool(used_prompt)
        t0 = time.perf_counter()
        segs = backend.transcribe(wav, initial_prompt=used_prompt, language=language)
        wall = time.perf_counter() - t0
    except Exception as exc:  # benchmark: record and continue
        row["error"] = f"{type(exc).__name__}: {exc}"
        return row
    text = "".join(s.text for s in segs)
    wer = M.word_error_rate(ref_text, text)
    hyp_timed = M.timed_words([(s.start, s.end, s.text) for s in segs])
    row.update({
        "wall_s": round(wall, 1),
        "rtf": round(wall / audio_s, 4),
        "x_realtime": round(audio_s / wall, 1),
        "words": len(M.normalize_words(text)),
        "wer": round(wer.wer, 4),
        "wer_detail": {"S": wer.substitutions, "D": wer.deletions, "I": wer.insertions,
                       "ref_words": wer.ref_words, "engine": wer.engine},
        "glossary": M.glossary_counts(text),
        "repeated_5grams": M.repeated_ngrams(text),
        # Whisper can keep decoding past the end of the audio (padding); WER
        # over segments that start inside the window separates that from
        # in-window errors.
        "wer_in_window": round(M.word_error_rate(
            ref_text, "".join(s.text for s in segs if s.start < audio_s)).wer, 4),
        "timestamps": timestamp_stats(segs, audio_s),
        "ts_vs_captions": M.timestamp_offsets(ref_timed, hyp_timed),
        "language": backend.last_language,
    })
    if text_dir:
        text_dir.mkdir(parents=True, exist_ok=True)
        safe = spec.replace("/", "_").replace(":", "__")
        (text_dir / f"{tag}__{safe}.txt").write_text(
            "\n".join(f"[{s.start:8.2f}-{s.end:8.2f}] {s.text.strip()}" for s in segs),
            encoding="utf-8",
        )
    del backend
    gc.collect()
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--start", required=True, help="H:MM:SS or seconds")
    ap.add_argument("--duration", type=float, default=600.0)
    ap.add_argument("--captions", required=True, type=Path)
    ap.add_argument("--info-json", type=Path)
    ap.add_argument("--title")
    ap.add_argument("--language", default="en")
    ap.add_argument("--backend", action="append", dest="backends",
                    help="name or name:model; repeatable")
    ap.add_argument("--out-json", type=Path)
    ap.add_argument("--text-dir", type=Path, help="write each transcript here for inspection")
    args = ap.parse_args()

    src = args.src.resolve()
    if str(src).startswith("/Volumes/"):
        sys.exit("refusing to read from /Volumes (removable media); work from a copy")
    if not src.exists():
        sys.exit(f"source not found: {src}")
    backends = args.backends or [
        "faster-whisper:large-v3", "faster-whisper:distil-large-v3",
        "parakeet-mlx", "mlx-whisper",
    ]
    start = M.parse_hms(args.start)
    ffmpeg = resolve_ffmpeg()
    prompt = build_prompt(args.info_json, args.title)
    lines = M.caption_lines_in_window(args.captions, start, args.duration)
    ref_text = " ".join(t for _, t in lines)
    ref_timed = M.timed_words(M.caption_spans(lines, args.duration))
    tag = args.start.replace(":", "")

    tmp = Path(tempfile.mkdtemp(prefix="bench_asr_"))
    wav = tmp / "window.wav"
    try:
        audio_s = extract_window(ffmpeg, src, start, args.duration, wav)
        if audio_s < args.duration - 1.0:
            sys.exit(f"window truncated: asked {args.duration}s, got {audio_s:.1f}s "
                     "(source shorter than start+duration?)")
        print(f"window {args.start} +{audio_s:.1f}s  ref words {len(M.normalize_words(ref_text))}",
              file=sys.stderr)
        result = {
            "src": str(src), "start": args.start, "start_s": start, "audio_s": audio_s,
            "prompt": prompt, "language": args.language,
            "reference": {"kind": "youtube-auto-captions", "lines": len(lines),
                          "words": len(M.normalize_words(ref_text)),
                          "glossary": M.glossary_counts(ref_text)},
            "runs": [],
        }
        for spec in backends:
            print(f"-> {spec}", file=sys.stderr)
            row = run_one(spec, wav, audio_s, prompt, args.language, ref_text,
                          ref_timed, args.text_dir, tag)
            print(json.dumps(row), file=sys.stderr)
            result["runs"].append(row)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # never leave the window behind

    out = json.dumps(result, indent=2)
    if args.out_json:
        args.out_json.write_text(out, encoding="utf-8")
    print(out)


if __name__ == "__main__":
    main()
