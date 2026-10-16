"""Segment-length cap (core/asr/segmenting.py) — WP-F5.

E3 regression: parakeet-mlx emitted one 229-word segment over ~123 s; the
transcript stores start times only, so P3 saw a > 120 s gap.
"""

from types import SimpleNamespace

from video2mdnotes.core.asr.base import clip_to_duration, finalize_segments, words_from_tokens
from video2mdnotes.core.asr.segmenting import split_long_segments, split_segment
from video2mdnotes.core.longasr import Part, Window, _segment_record, stitch
from video2mdnotes.core.transcriber import Segment, Word


def _norm(text):
    return " ".join(text.split())


def _assert_valid(parts, orig, max_s=None, max_words=None):
    assert _norm("".join(p.text for p in parts)) == _norm(orig.text)
    assert parts[0].start == orig.start
    assert parts[-1].end == orig.end
    for a, b in zip(parts, parts[1:]):
        assert a.end <= b.start, (a, b)
    for p in parts:
        assert orig.start <= p.start <= p.end <= orig.end
        if max_words is not None:
            assert len(p.text.split()) <= max_words
        if max_s is not None:
            assert p.end - p.start <= max_s + 1e-9 or len(p.text.split()) == 1


def _timed_segment(n_sentences=12, words_per=19, word_s=0.5, start=100.0):
    """Many sentences glued into one segment, with exact word timings."""
    words, t = [], start
    for k in range(n_sentences):
        for i in range(words_per):
            w = f"w{k}_{i}"
            if i == words_per - 1:
                w += "."
            words.append(Word(start=t, end=t + word_s * 0.8, text=" " + w))
            t += word_s
    text = "".join(w.text for w in words)
    return Segment(start=start, end=words[-1].end, text=text, words=words)


def test_long_segment_with_word_timings_splits_at_sentence_ends():
    seg = _timed_segment()  # 228 words, ~114 s — the E3 shape
    parts = split_segment(seg, max_s=20.0, max_words=60)
    assert len(parts) > 5
    _assert_valid(parts, seg, max_s=20.0, max_words=60)
    # Every cut but the last lands after a sentence-ending word.
    assert all(p.text.rstrip().endswith(".") for p in parts)
    # Times are the real word times, not a proportional guess: each later
    # part starts exactly at its first word's start.
    by_text = {w.text.strip(): w for w in seg.words}
    for p in parts[1:]:
        first = p.text.split()[0]
        assert p.start == by_text[first].start
    # Parts keep their own timings for a later re-split.
    assert all(p.words for p in parts)
    assert sum(len(p.words) for p in parts) == len(seg.words)


def test_long_segment_without_timings_splits_proportionally_at_punctuation():
    text = " " + " ".join(
        f"Sentence {k} has a few words in it, and then some more." for k in range(20))
    seg = Segment(start=10.0, end=130.0, text=text)  # 240 words, 120 s
    parts = split_segment(seg, max_s=20.0, max_words=60)
    assert len(parts) >= 6
    _assert_valid(parts, seg, max_s=20.0, max_words=60)
    assert all(p.words is None for p in parts)
    assert all(p.text.rstrip().endswith((".", ",")) for p in parts)
    # Proportional: a part's share of time tracks its share of words.
    n = len(text.split())
    for p in parts:
        expect = 120.0 * len(p.text.split()) / n
        assert abs((p.end - p.start) - expect) < 120.0 / n + 1e-6


def test_no_punctuation_still_splits_at_word_boundaries():
    seg = Segment(start=0.0, end=60.0, text=" " + " ".join(["word"] * 150))
    parts = split_segment(seg, max_s=20.0, max_words=60)
    _assert_valid(parts, seg, max_s=20.0, max_words=60)
    assert all(set(p.text.split()) == {"word"} for p in parts)


def test_short_segment_is_untouched_and_split_is_idempotent():
    short = Segment(start=0.0, end=3.0, text=" Hello there.")
    assert split_segment(short, 20.0, 60)[0] is short
    once = split_long_segments([_timed_segment(), short], 20.0, 60)
    twice = split_long_segments(once, 20.0, 60)
    assert [(s.start, s.end, s.text) for s in once] == [(s.start, s.end, s.text) for s in twice]


def test_mismatched_word_timings_fall_back_to_proportional():
    seg = _timed_segment()
    seg = seg.model_copy(update={"text": seg.text + " edited"})
    parts = split_segment(seg, 20.0, 60)
    _assert_valid(parts, seg, max_s=20.0, max_words=60)
    assert all(p.words is None for p in parts)


def test_parakeet_tokens_become_words_and_split_inside_clipped_audio():
    # Sub-word tokens: " Hel" "lo" " world." ... (SentencePiece convention).
    toks, t = [], 0.0
    for k in range(100):
        toks.append(SimpleNamespace(text=" ab", start=t, end=t + 0.2))
        toks.append(SimpleNamespace(text="cd." if k % 10 == 9 else "cd", start=t + 0.2, end=t + 0.4))
        t += 0.5
    words = words_from_tokens(toks)
    assert len(words) == 100 and words[0].text == " abcd"
    assert words[0].start == 0.0 and words[0].end == 0.4
    seg = Segment(start=0.0, end=words[-1].end, text="".join(w.text for w in words), words=words)
    # Audio ends at 30 s: clip first, then split — nothing past the audio.
    parts = finalize_segments([seg], duration=30.0)
    assert parts and parts[-1].end <= 30.0
    assert all(p.end - p.start <= 20.0 for p in parts)
    assert _norm("".join(p.text for p in parts)) == _norm(seg.text)


def test_clip_keeps_word_timings():
    seg = Segment(start=0.0, end=5.0, text=" a b", words=[Word(start=0, end=1, text=" a"),
                                                          Word(start=1, end=5, text=" b")])
    (clipped,) = clip_to_duration([seg], 3.0)
    assert clipped.end == 3.0 and clipped.words == seg.words


def test_checkpoint_roundtrip_keeps_words_and_stitch_caps(tmp_path):
    from pathlib import Path

    seg = _timed_segment(start=5.0)
    part = Part(index=0, path=Path("a.mov"), duration_s=200.0, offset_s=50.0)
    w = Window(file_idx=0, window_idx=0, ext_start=0.0, ext_end=200.0,
               core_start=0.0, core_end=200.0)
    rec = _segment_record(seg)
    assert len(rec["words"]) == len(seg.words)
    out = stitch([(part, w, {"segments": [rec]})])
    assert len(out) > 5
    assert out[0].start == 55.0
    assert all(s.end - s.start <= 20.0 for s in out)
    starts = [s.start for s in out]
    assert starts == sorted(starts)
    # Old checkpoints (no words) are capped too, proportionally.
    legacy = {"start": 0.0, "end": 120.0, "text": " " + " ".join(["x."] * 229)}
    out2 = stitch([(part, w, {"segments": [legacy]})])
    assert max(s.end - s.start for s in out2) <= 20.0
