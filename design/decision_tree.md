---
note_type: reference
title: "video2notes-v2 — decision tree"
tags: [from-octobob, claude, architecture, video2notes]
updated: 2026-08-25
---

# Decision tree

What happens to an input, and **where money gets spent**. Every `$` below is a
paid or subscription-consuming call; everything else is free.

## Top level: what did you point at?

```
input
├── URL ─────────────────────► process
├── .epub / .pdf ────────────► book
├── audio / video file ──────► media
└── a directory of books ────► catalog        (free, always start here)
```

## process — a URL

```
URL
└── yt-dlp probe
    ├── playlist?  ──► per-video loop, --group-by-playlist for one directory
    ├── unsupported URL?
    │   └── scrape page for embeds ──► retry each embed
    └── single video
        │
        ├── MANUAL captions available?          ◄── free signal
        │   ├── yes ──► use them.  NO WHISPER.  provenance: "captions (manual, en)"
        │   └── no  ──► download audio ──► Whisper       (CPU minutes)
        │        Auto-captions are NEVER used. Machine ASR from YouTube is
        │        not better than local Whisper and pretends to be human-authored.
        │
        ├── transcript written to disk  ◄── BEFORE summarizing, always
        │
        ├── EXTRACT_FRAMES?
        │   └── yes ──► scene-detect ──► dedupe (phash + burst) ──► rank by dwell
        │        └── per frame: macOS Vision OCR              ◄── free signal
        │            ├── enough words? ──► use OCR text.  no VLM.
        │            ├── looks like burned-in subtitles? ──► skip
        │            ├── low contrast (stdev < 30)? ──► skip (blank/transition)
        │            └── else ──► $ vision model, capped by VLM_MAX_FRAMES
        │
        └── $ summarize  ──► failover chain
            └── exhausted? ──► --on-exhaustion:
                 wait (default) · metered · local · stop · ask
                 NEVER silently escalates to paid.
```

## book — EPUB or PDF

```
file
├── --extract-only? ──► print structure + passage count. FREE. STOP.
│      Use this habitually. It caught both PDF structure bugs.
│
├── EPUB                              PDF
│   ├── encrypted? ──► DRMError       ├── password? ──► EncryptedPDFError
│   ├── images only? ──► ImageOnly    ├── no text layer? ──► ScannedPDFError
│   ├── spine = order                 │      (OCR is out of scope)
│   ├── nav.xhtml / toc.ncx = names   ├── bookmarks? ──► real structure
│   └── locator: "ch. 7"              ├── else font-size heuristic ──► inferred
│       NEVER a page number           └── locator: "p. 214"
│
├── chunk: never straddle a chapter, ≤12,000 words
│      one chunk = one locator = every claim attributable
│
├── $ per passage  (~17 calls, ~13 min per book)
│      code blocks are COPIED, not described
│
└── reduce: ONE round of batches (6), then CONCATENATE
       never recursive — a second round returns chat, not notes
       reduce fails? ──► passage notes are kept, never discarded
```

## media — a local file

```
file
├── probe with ffmpeg  ◄── reads the REAL codec, not the extension
│   ├── DRM? ──► EncryptedMediaError
│   ├── no audio stream? ──► NoAudioStreamError
│   └── undecodable? ──► UnreadableMediaError ("may need vendor export")
│
├── --probe? ──► print and STOP. FREE.
├── convert to 16 kHz mono wav  (ffmpeg, -vn drops video)
├── Whisper                     (CPU minutes)
├── transcript written          ◄── BEFORE summarizing
└── $ summarize
```

No network access at any point. A test asserts it.

## catalog — the free layer

```
catalog index --corpus <dir>       65s for 386 books. NO model, NO network.
│   unreadable file? ──► recorded as a row with its reason, run continues
│
catalog overview                   what do I own
catalog search "..."               ◄── the interesting one
│   ├── lexical BM25   (~1 ms, all 90,791 sections)
│   ├── semantic       (~20 ms, local vectors, $0)
│   ├── fuse by Reciprocal Rank Fusion — rank position only,
│   │     because BM25 and cosine are not on a common scale
│   └── rerank top ~60 with a local cross-encoder (~1 s)
│         bounded because 90,791 pairs would take half an hour
│
catalog embed                      one-time, ~8.4 min, local, $0
```

**Everything in this section is free.** That is the point: it is the filter in
front of the 340 hours that summarizing the whole corpus would cost.

## Where to intervene

| Want to spend less? | Do this |
|---|---|
| Before any book run | `book --extract-only` |
| Before any transcription | `media --probe` |
| Before deciding what to read | `catalog search` |
| Overnight / unattended | `--on-exhaustion stop` |
| Visuals off | `EXTRACT_FRAMES=false` |
