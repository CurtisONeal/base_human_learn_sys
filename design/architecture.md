---
note_type: reference
title: "video2notes-v2 — architecture"
tags: [from-octobob, claude, architecture, video2notes]
updated: 2026-08-25
---

# Architecture

Mirror of repo notes. Canonical: `repos/video2notes-v2/design/`.

## The one idea

**Free signals decide when to spend money.** Everything else follows from it.

| Free signal | Gates | Saves |
|---|---|---|
| Manual captions exist? | Whisper transcription | minutes of CPU per video |
| OCR yield on a frame | The vision model | a paid call per frame |
| Publisher's own TOC | Reading a book at all | ~13 min + 17 calls per book |
| Lexical + vector search | The reranker | ~30 min per query |

The pattern is always the same: something already present in the material tells
you whether the expensive step is worth taking.

## Shape

```
   ┌── media ──────────────────────────┐
   │  YouTube / playlist  (yt-dlp)     │
   │  local audio/video   (ffmpeg)     │──┐
   │      ↓ captions-first, else Whisper │  │
   └─────────────────────────────────────┘  │
                                            ├──►  SourceText  ──►  summarize  ──►  notes
   ┌── documents ────────────────────────┐  │      (the seam)      (failover      (+ locators)
   │  EPUB  (zipfile + html.parser)      │──┘                       chain)
   │  PDF   (pymupdf)                    │
   │      ↓ publisher TOC → sections     │
   └─────────────────────────────────────┘
                     │
                     └──►  catalog  ──►  hybrid search  ──►  (WP-05 reading plan)
                          (free)         (free, local)
```

## SourceText — the convergence point

Every source produces one type. The split that matters is **how you point at a
claim**:

```
media     segments[]   "at 04:12"           time is the locator
document  sections[]   "ch. 3, §Indexing"   structure is the locator
```

Both lists are optional; a source fills in whichever it has. This is why the
visual-extraction plugin is *naturally* inapplicable to a book — a book has no
`segments` to key on, so it skips the frame path by having nothing to match
rather than by a special case in `main()`.

## Locator honesty

The system's value is that its claims can be checked. So a locator never claims
more precision than the format supports:

```
epub (nav.xhtml)   epub (toc.ncx)   epub (none)
pdf (bookmarks)    pdf (headings)   pdf (pages only)
```

**An EPUB has no pages and never prints one.** It prints `ch. 7`, or the section
name when the publisher did not number the chapter. A PDF prints `p. 214`.
`structure_is_inferred` is set whenever structure was guessed rather than read.

## Spend control

- `LLM_MODELS` is a **failover chain, not a quality ladder** — entry 1 does
  essentially all the work; later entries fire only on error.
- Subscription (keychain OAuth via the `claude` CLI) vs metered (litellm) are
  distinct paths. **Exhaustion never silently escalates to metered.**
- `--on-exhaustion wait|metered|local|stop|ask`, default `wait`. An unattended
  run must not start spending because nobody was there to answer.
- `claude -p` is an **agent**, not a completion endpoint: it runs with
  `--disallowedTools` and a scratch cwd, because transcripts are untrusted
  third-party text.

## Current state (2026-08-25)

| | |
|---|---|
| Tests | 252 |
| Sources | YouTube, playlists, embeds, EPUB, PDF, local audio/video |
| Catalog | 386 books · 39.7M words · 90,791 sections — indexed in 65s |
| Semantic | 90,791 local vectors, 34.9 MB, $0 |

Not built: reading plans (WP-05 plan stage), glossary, learning-notes stage,
scaffolding, kata, vault read/write.
