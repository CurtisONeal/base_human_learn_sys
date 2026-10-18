# Learning packages — grounded notes inside the Obsidian vault (decided 2026-10-17)

## Problem
Notes cite their evidence (`[§Indexing]`, `[ch. 7]`, `[p. 214]`, `[0:12:34]`) but nothing in Obsidian can
follow those references: EPUBs can't be opened or deep-linked, PDFs live outside the vault, video times
are plain text. Grounding links are required, not optional.

## Decision (user-approved)
One package per studied source, inside the vault:

```
<vault>/010_learning_system/by_subject/<subject>/<source-slug>/
  notes.md             summary; every citation is a working relative link
  text/ch07.md …       EPUB/PDF sections extracted to markdown (headings kept → #anchors)
  images/…             diagrams extracted from the source, embedded in notes.md
  source_meta.json     canonical corpus path, fingerprint (catalog id), provenance
<vault>/__sources/<subject>/<source-slug>/<original>.pdf|.epub   ← binaries, EXCLUDED from Obsidian Sync
```

Link forms (relative, markdown links — vault setting `useMarkdownLinks: true`):
- PDF page: `[p. 214](../../../../__sources/<subject>/<slug>/<file>.pdf#page=214)` (opens Obsidian's PDF viewer)
- Section: `[ch. 7 §Indexing](text/ch07.md#indexing)`
- YouTube time: `[0:12:34](https://youtu.be/<id>?t=754)`
- Local recording time: `[0:12:34](transcript.md#^t0-12-34)` (block id on the transcript line)
- Figures: `![Figure 3-2 …](images/fig-3-2.png)` placed in the chapter that discusses them

Subjects so far: `azure_identity` (SC-900, Azure Master Class Pt 1–2, Entra note), `claude_architecture`
(CCA-F guide + course), `llm_from_scratch` (Karpathy), `community_events` (CLE AI meetup, ClePy).

## Storage
- Whole corpus + catalog DB on the 20 TB drive on the main machine; replicated (second drive or cloud) with
  periodic checksum verification. Only studied sources get a package in the vault.
- Main machine hosts corpus, catalog DB, GTD app and vault close together. Code on GitHub.
- User action: Obsidian → Settings → Sync → excluded folders → add `__sources`.
