# Master design: the Collector and the learning loop

**Status:** supersedes `design/CONSOLIDATED_DESIGN.md` (kept as the dated source record this was built from).
**Created:** 2026-08-23. **Repo:** `/Users/s_admin/Documents/agent_set_up/repos/video2notes-v2`.
**Governing constraint:** *usability during construction.* Every phase must leave the user able to do something he could not do before. **A phase that only refactors is not a phase.**

---

## 1. Orientation — read this cold

One pipeline that turns **any material you own or can legally reach** into markdown notes, and a second layer that turns those notes into something you actually learn. The capture half is **the Collector**; the learning half is **the learning loop**. They are joined by a handoff of files on disk, not a function call.

The repo is named `video2notes-v2` for historical reasons. Video was the **first vertical slice**, not the point. The intent name from the original 2026-01-14 hand-drawn design is `everything2mdnotes`.

### The two source families

| | MEDIA family | DOCUMENT family |
|---|---|---|
| Members | YouTube, paywalled video, meeting audio, DVD, audiobook | epub, pdf, docx, md, html |
| Extra stage | audio → transcript (captions or Whisper) | none — text extraction only |
| Cost | download + transcription + optional frames/OCR/vision | read a file |
| Fragility | yt-dlp chases a moving target; ffmpeg; macOS-only OCR | format quirks, DRM |
| Built today | **yes** (YouTube only) | **no — entirely unbuilt** |

Both converge on **one summarizer**. That convergence is the Phase 1 seam. The DOCUMENT family is the cheaper half by a wide margin and is completely missing.

### Components vs plugins

A **component** has its own purpose. A **plugin** cannot justify its own existence.

| Component | Purpose alone | Built? |
|---|---|---|
| Source capture | Material → text + provenance | Video only |
| **Research / collection + summary** | "What is this, what did it say, is it worth my time" | **Yes — this is the repo today** |
| **Corpus triage** *(new)* | "Across everything I own, what do I read to learn X" | No |
| Learning organisation | Material → shelf → core → mechanics | No (skill defines it; nothing runs it) |
| Practice / retention | Retrieval practice, spaced review, kata | No |
| Teach-back | Explanation ladder, demo plan, learner checks | No |

Plugins: visual extraction, frame ranking/guards/profiles, captions-first, domain prompt selection, concept maps. **Glossary is a plugin to both** research/summary and learning.

Research+summary has **two callers** — a human, and a learning stage. That is what makes it a component rather than a stage.

### The feedback loop

```
Collector ──▶ notes ──▶ Learning / Teach-back
    ▲                          │
    └──── research request ◀───┘   (a file in a queue, like any source URL)
```

The learning layer **does not call extraction**; it **emits a research request** the Collector consumes as ordinary input. Trying to teach something is precisely where you discover what a source never covered — swallowing that is a design failure.

| Gap | Surfaces during | Response |
|---|---|---|
| Coverage — never addressed | notes quality check | find a source that does |
| Comprehension — can't be explained from notes | teach-back | find a *better* source |
| **Best practice — outdated or contradicted** | quality check | **surface conflict with both citations; never auto-resolve** |
| Vocabulary — used, never defined | glossary | look up; check Obsidian first |
| Prerequisite — depends on unlearned material | scaffolding | reorder, or spawn a spike |

### The load-bearing principle

**Free signals gate expensive ones.** Captions gate Whisper. OCR gates the vision model. Ranking decides which frames are worth paying for. Subscription capacity is never silently traded for metered billing. This is why corpus triage is designed lexical-first and reads headings before bodies.

---

## 2. What exists today

| Capability | State |
|---|---|
| YouTube probe / playlist / resume | Built, tested |
| Captions-first (manual only, speech guard, CJK) | Built (ADR-038) |
| Local Whisper fallback | Built |
| Summarization chain, subscription-first, no-surprise-spend | Built (ADR-037) |
| Domain prompt selection (YAML library) | Built |
| Page-embed scraping (public embeds only) | Built |
| Visual layer: keyframes, OCR, vision tier, ranking, guards | Built, **ahead of intended order** (ADR-040) |
| Tests | 145 |
| **DOCUMENT family** | **Nothing** |
| **Phases 1, 2, 2.5, 3** | **Unstarted** |

~3,300 lines of Python. The visual layer is roughly a third of it and is **parked by the phase plan**. Not wasted — it rescued a silent video from a 94-byte placeholder — but **do not extend it**.

---

## 3. Phases — MUST vs NICE

"Must" means: *the phase delivers no standalone value without it.*

### Phase 1 — The source seam + the DOCUMENT family
*Deviation: the original plan parks EPUB/PDF as "later". Un-parked. Reasons in §5.*

| | Item |
|---|---|
| MUST | `SourceText` type carrying text + provenance + optional structure, replacing `TranscriptResult` in the summarizer signature |
| MUST | At least one non-media extractor through that seam — otherwise the seam is unverified speculative generality |
| MUST | EPUB + PDF with **structure preserved** (chapter tree + locators), not a flat blob |
| MUST | Chunked map-reduce for book-length input |
| MUST | Per-claim locator provenance (page/chapter) surviving into notes |
| NICE | docx, plaintext, RTF; BeautifulSoup HTML; local media file input; rename to `collector` |

### Phase 1.5 — Corpus triage *(new; §6)*

| | Item |
|---|---|
| MUST | Catalog: walk corpus, extract metadata + TOC tree + section word counts. **Zero LLM cost.** |
| MUST | Lexical topic locate — reuses `_rate_per_1000` already in `core/domains.py` |
| MUST | Reading plan: ranked sections, prerequisite-ordered, one-line reason each, plus "skipped because" |
| MUST | LLM step reads **headings + first N words of top-K only**, never full bodies |
| NICE | Semantic embeddings when lexical is flat; cross-corpus concept index; coverage gaps |

### Phase 2 — Learning output from text alone

| | Item |
|---|---|
| MUST | A `learn` command: existing artifact → `with-learning-science`-shaped notes |
| MUST | The skill's structural contract honoured |
| MUST | Confidence separation: source-backed vs inference vs open question (ADR-037) |
| NICE | Auto learning-job classification; the skill's validator as a quality gate; concept maps |
| **NOT** | Screenshots, GUI walkthroughs, visual extraction — excluded by definition |

### Phase 2.5 — Pedagogical scaffolding

| | Item |
|---|---|
| MUST | Reorganise concepts: big-picture shelf → bare-bones core → deeper mechanics |
| MUST | Prerequisite ordering across a set of notes |
| NICE | Surface/deep and conceptual/procedural tagging; spiral revisit markers |
| **NOT** | Learner-state personalization — parked, and needs data that does not exist |

### Phase 3 — Kata practice

| | Item |
|---|---|
| MUST | One small end-to-end project per topic: worked example → partial → independent |
| MUST | A verifiable done-condition |
| NICE | Recall cards in an Anki-importable form |
| **NOT** | A quiz engine. **NOT** an SRS scheduler. Anki exists; emit into it. |

### Standing — Maintenance

| | Item |
|---|---|
| MUST | yt-dlp bumped opportunistically whenever the lockfile is touched (ADR-042) |
| NICE | Weekly freshness check: update → **integration** suite → alert |

---

## 4. Usable at every step

| After | What he can now do | Usable alone? |
|---|---|---|
| **today** | YouTube video/playlist → structured notes, captions-first, resumable | Yes — why the Claude courses work now |
| **WP-01** seam + text/md/html | Point at a local `.md`/`.txt` or public article URL. **Covers the HF docs, free HF course pages, blogs and papers — the material around the Udemy course that is not login-gated** | **Yes** |
| **WP-02** epub + pdf | Owned book or paper → chapter-scoped notes with page citations | **Yes — largest single utility jump** |
| **WP-03** local media file | Drop a lecture/meeting/audiobook on disk and transcribe | Yes (small) |
| **WP-04** catalog | "What do I own, and what is in it" — browsable index. No LLM cost | **Yes** |
| **WP-05** locate + plan | **"What do I read to learn graph RAG" → ranked, prerequisite-ordered plan across the corpus** | **Yes — the stated blocker** |
| **WP-06** glossary | "Terms this used but never defined", flagged for the Obsidian pass | Yes |
| **WP-07** learn stage | Any summary → study notes with recall prompts and transfer tasks | Yes |
| **WP-08** scaffolding | A *sequence* across a playlist or book — what to learn first | Yes |
| **WP-09** research requests | Gaps found while learning become queued Collector jobs | **No, alone** |
| **WP-10** kata | Practice project with a fading-support ladder | Yes |
| **WP-12** deps check | Stop losing an hour to a 403 that is really a stale pin | Yes (ops) |

**Resequencing forced by this table:** the original Phase 1 as written ("separate capture from interpretation") is a **pure refactor** delivering nothing usable. *That is why it has stayed unstarted for six months.* Bundling it with a real second consumer (WP-01) makes the refactor a side effect of a feature. **Never schedule a seam refactor as its own session.**

WP-09 is the only package with no standalone value. Schedule it *after* WP-07.

---

## 5. Build order

| # | WP | Why here |
|---|---|---|
| 1 | **WP-01** seam + text/md/html | Cheapest proof of the seam; no new deps; immediately usable on HF companion material |
| 2 | **WP-02** epub + pdf | Biggest utility jump per session; unblocks WP-04/05; cheapest family (no download, Whisper, ffmpeg, vision spend, or yt-dlp tax) |
| 3 | **WP-04** catalog | Free. Browsable index the day it lands |
| 4 | **WP-05** locate + plan | Answers the stated blocker |
| 5 | **WP-07** learn stage | Cheap — the skill exists; this is wiring, not invention |
| 6 | **WP-06** glossary | Plugin to two components; Phase 2.5 prerequisite; useful on today's summaries |
| 7 | **WP-08** scaffolding | Needs WP-07's concept output |
| 8 | **WP-03** local media | Slot anywhere — near-free, ~1 hour |
| 9 | **WP-09** research requests | Needs WP-07 to produce gaps |
| 10 | **WP-10** kata | Phase 3 |
| 11 | **WP-11** Obsidian pass | **Blocked** on vault reachability (D3) |
| 12 | **WP-12** deps check | When the next 403 costs an hour |

### The three deviations, argued

**(a) Un-park EPUB/PDF.** The original parked it because visual/GUI complexity was the perceived risk — that reasoning does not apply. Documents are the *cheapest* family. And a seam refactor with exactly one implementation cannot be verified: the way you prove capture is separated from interpretation is to put a second thing through it.

**(b) Insert corpus triage before Phase 2.** It is the only unbuilt thing addressing a *stated* blocker. Phase 2 improves notes he can already produce; Phase 1.5 unblocks material he currently cannot approach at all.

**(c) Phase 2's first increment is skill-wiring, not new Python.** `with-learning-science` already encodes the taxonomy, markdown contract, checklist and a validator. Treat any proposal to reimplement the pedagogy in Python as scope creep.

**Not deviated from:** the visual layer stays parked. Adding to it now repeats the mistake that produced it.

---

## 6. Corpus triage, designed

> *"I can't digest all the epubs to decide what is needed to learn a topic — example graph rag — without some perspective, and maybe an index."*

The failure mode of the obvious approach matters: summarizing 40 books costs a fortune and leaves 40 summaries to read. **It converts one unreadable pile into a smaller unreadable pile.** The question is *selection*: which 6 sections of which 3 books, in what order.

### Is it separable?

| Depends on | Verdict |
|---|---|
| Document extraction (WP-02) | **Not separable — hard prerequisite** |
| The summarizer | Separable — triage produces a *plan*, not summaries |
| The learning layer | Fully separable — useful with nothing downstream |
| The visual layer | Irrelevant |
| Research-request loop | Separable, composes later ("nothing you own covers X" is a coverage gap) |

**Conclusion: a genuine standalone component with exactly one prerequisite — and that prerequisite is already the highest-priority unbuilt work for independent reasons.** Not premature.

### Four stages, free gating expensive

| Stage | Cost | What |
|---|---|---|
| **1. Catalog** | Free | Walk `CORPUS_DIR`. Per file: format, title, author, **TOC tree** (EPUB spine/nav; PDF outline; heading heuristics fallback), section word counts, text |
| **2. Locate** | Free | Score every *section* by term density. Answers "which books even mention graph RAG" with zero spend |
| **3. Triage** | Cheap, bounded | Send **headings + first N words of top-K only**. Get read/skim/skip, a reason each, prerequisite order |
| **4. Read** | Optional | Feed only selected sections to the existing summarizer |

**Lexical before embeddings:** `graph rag`, `HNSW`, `LoRA` are lexically distinctive. Embeddings add a dependency, a download or metered spend, and index-rebuild cost — for a gain only real on *conceptual* queries. Build lexical, **measure where it fails**, then decide. Same discipline as OCR-before-VLM.

### Layout

```
src/video2mdnotes/core/corpus/{__init__,catalog,store,locate,plan}.py
prompts/triage/reading_plan.md
```

| Command | Effect |
|---|---|
| `index <dir>` | Build/refresh catalog. No LLM |
| `catalog [--format epub]` | Print what is indexed |
| `plan "<topic>"` | Emit `<topic>-reading-plan.md` |
| `read <plan.md>` | Summarize only selected sections |

The plan carries, per section: book, chapter, locator, word count, reading time, term-density evidence, a reason, prerequisite position — **plus a "considered and skipped" table (this is what builds trust in the ranking)** and an "apparently not covered by anything you own" section.

### The honest blocker

The corpus is almost certainly on the **laptop**, not this Mac mini. Corpus triage is unusable from here until D1 is resolved. **This is the single largest risk to the highest-value work package.**

---

## 7. Component work packages

Each is one session, and becomes its own doc under `design/components/`.

### WP-01 — Source seam + text / markdown / HTML — **DONE 2026-08-27**
- **Goal** Introduce `SourceText` as the canonical summarizer input, and prove it by putting non-media text through it.
- **In/Out** local `.md`/`.txt` or public article URL → same notes shape as today.
- **Built** `core/documents/{plaintext,html,_htmltext}.py`. `SourceText`/`Section` already existed from WP-02 — this WP's real remaining scope turned out smaller than estimated. `_htmltext.py` is new: EPUB's HTML parser was extracted into a shared module rather than duplicated, since a local `.html` file and a fetched article are the same parsing problem as EPUB's XHTML wearing a different container.
- **Changed** `main.py`: `process` now checks for a local document file before assuming a URL, and falls back to `html.extract_url()` when yt-dlp raises `DownloadError` and no video embed is found either — the same cheapest-signal-first cascade as embed scraping, one step further. `book`'s per-file logic was refactored into a shared `_summarize_and_write_document()` used by both commands.
- **Accept** 1. `process ./notes.md` summarizes ✅. 2. `process <public article>` summarizes ✅ (network mocked in tests per the established webfetch boundary; parsing is real). 3. YouTube path byte-identical ✅ — explicit regression test plus the full existing suite unchanged. 4. ~~145 tests~~ 25 new tests, 297 total. 5. Document `SourceText` skips the visual path ✅ (already covered by WP-02's `test_document_has_no_timeline_so_visual_path_cannot_apply`).
- **Bugs found building it** (see FIXED_BUGS.md): a real vault note had a doubled markdown heading marker (`# # Real Title`) — an export artifact — which produced a locator with a literal `#` in it; and the `book`/`process` refactor initially conflated "ordinary failure" with "exhaustion under a non-wait policy" in a shared return value, which would have made an ordinary per-file error silently stop an entire batch whenever `--on-exhaustion` was set to anything but `wait` — caught before it shipped, not after.
- **Not in scope** docx, chunking changes, any change to captions/Whisper/frames/OCR/ranking/domains.

### WP-02 — EPUB + PDF with structure and chunked summarization
- **Goal** Read owned books/papers preserving chapter structure and page provenance; summarize book-length text without a context error.
- **New** `core/documents/{epub,pdf}.py`; `core/chunking.py`; tests; **real-file fixtures** under `tests/fixtures/documents/`.
- **Deps** `ebooklib` (or zipfile+lxml over the OPF spine), `pymupdf`/`pypdf`, as an optional `[documents]` extra mirroring `[ocr]` — **and update the readme sync command in the same commit**, because a bare `uv sync` silently strips extras (FIXED_BUGS).
- **Accept** 1. Real EPUB → section tree matching its TOC. 2. Real PDF with bookmarks → same; without bookmarks falls back to heuristics *and says so in provenance*. 3. 300+ page PDF summarizes without context error. 4. Every claim carries a chapter/page locator. 5. DRM input fails clearly and **does not attempt circumvention**. 6. **Fixture tests use real files, not mocks** — mocked tests already passed 136/136 while every download 403'd.
- **Not in scope** docx, mobi/azw, OCR of scanned PDFs, the corpus index, embeddings.
- **Cost note** Chunked map-reduce over a book may consume more capacity than a whole playlist. Interacts with `ON_EXHAUSTION` — see D5.

### WP-03 — Local media file input
- **Goal** Transcribe audio/video already on disk. `transcribe_audio()` already takes a `Path` — most is built.
- **Accept** An `.m4a` produces the same artifacts a YouTube video does, `origin` records the file path, **no network access occurs**.
- **Not in scope** DVD ripping, diarization, audiobook chapter splitting. **Size** half a session; covers meeting audio + audiobooks from the original diagram at near-zero cost.

### WP-04 — Corpus catalog and store
- **Goal** A free browsable index: metadata + TOC + section word counts. Zero LLM cost.
- **New** `core/corpus/{catalog,store}.py`; tests. **Deps** WP-02 (hard); **D1 must be answered first**.
- **Accept** 1. Indexing N books makes **no LLM call and no network access** — assert it. 2. Re-indexing is idempotent, only re-reads changed files. 3. A file that fails to parse is recorded with a reason and **does not abort the run** (the `None` playlist entry lesson). 4. `catalog` prints title/author/format/sections/words.
- **Not in scope** Ranking, topic matching, any LLM, embeddings, a UI.

### WP-05 — Topic locate and reading plan
- **Goal** "What do I read to learn X" → ranked, prerequisite-ordered plan.
- **New** `core/corpus/{locate,plan}.py`; `prompts/triage/reading_plan.md`; tests. **Deps** WP-04.
- **Accept** 1. Locate is **pure lexical, no LLM call** — assert. 2. A test asserts the LLM payload **never contains a full section body**. 3. Plan contains selections+locators+reasons, a "considered and skipped" table, prerequisite order, and a "not covered" section. 4. **Run against the real corpus for `graph rag` and have the user judge it — that is the acceptance test that matters; unit tests only prove it did not cheat.**
- **Not in scope** Embeddings (deferred pending measured lexical failure), summarizing selections (that is `read`), cross-book dedup.

### WP-06 — Glossary / vocabulary plugin
- **Goal** Terms a source used but never defined, marked for the Obsidian pass.
- **Out** A `## Vocabulary` block: term, definition-if-in-source, `[new]`/`[undefined]`.
- **Deps** WP-01. Independent of the learning layer — a plugin to both.
- **Accept** 1. Runs against an archived summary without re-transcribing. 2. Terms defined *in the source* marked distinctly from terms merely used. 3. **Nothing invented** — undefined is reported as undefined (ADR-035's `- None` discipline).
- **Not in scope** Reading/writing the vault (WP-11, blocked); cross-source dedup.

### WP-07 — Learning-notes stage
- **Goal** Existing artifact → `with-learning-science`-shaped study notes. Phase 2, text only.
- **Out** `<slug>/learning/<slug>.learning.md`. **Deps** none new — **can be built before WP-01**.
- **Accept** 1. Output matches the skill's section contract. 2. The skill's `validate_learning_notes.py` passes. 3. Confidence/inference separation present. 4. No screenshots, no visuals, no Mermaid.
- **Not in scope** Reimplementing pedagogy in Python — this is prompt/skill wiring. Concept maps, scaffolding, practice.

### WP-08 — Pedagogical scaffolding
- **Goal** Reorganise concepts across a *set* of notes; prerequisite ordering. **Deps** WP-07.
- **Accept** Given the 8-video 3Blue1Brown set already in `previous_run_results/`, produce an order plus dependency rationale; mark spiral recurrence.
- **Not in scope** Learner-state personalization (parked).

### WP-09 — Research request queue
- **Goal** Gaps become durable queued Collector inputs. **Deps** WP-07 (hard — nothing produces gaps before it).
- **Accept** 1. All five gap kinds representable. 2. **A best-practice conflict is recorded with both citations and never auto-resolved.** 3. **An import test asserts the learning layer never imports `core/downloader`, `frames`, or `visuals`** — that is the whole architectural point.
- **Not in scope** Building a broker — reuse the `inbox/wip/done` shape proven in `repos/agent-tasks/`. Merging with the voice-chat roadmap (separate project, shared mechanism only).

### WP-10 — Kata practice generator
- **Deps** WP-07, ideally WP-08. **Accept** verifiable done-condition; three rungs of decreasing support; recall prompts in Anki-importable markdown.
- **Not in scope** Quiz engine, SRS scheduler, grading.

### WP-11 — Obsidian source pass *(BLOCKED)*
- **Blocked on** the vault at `/Users/curtisoneal/Documents/syncing_vault2_clean_2026` (laptop), unreachable from here. See D3.
- **Not in scope** Writing into the vault until read-only behaviour is proven and approved.

### WP-12 — Dependency freshness check
- **Goal** Stop paying the yt-dlp diagnosis tax (two breaks in four days).
- **Out** A script that updates the lockfile, syncs **with extras**, runs the **integration** suite (units mock the network and passed 136/136 while everything was 403ing), and alerts.
- **Accept** Detects a deliberately stale pin; **refuses to run a bare `uv sync`**.
- **Not in scope** Auto-committing the lockfile without a human seeing the diff.

---

## 8. What NOT to build

| Don't | Why |
|---|---|
| **Login-gated circumvention (Udemy)** | Standing decision. Independently measured as "less signal — or immediate utility". WP-01/02 route around it legitimately: HF docs, the free HF course, and papers are open |
| **More visual-layer work** | Parked, and already built ahead of order. Extending it repeats the mistake |
| **A quiz engine or SRS scheduler** | Anki exists and is better. Emit cards; do not schedule them |
| **A learner-state model** | Parked; requires usage data that does not exist |
| **Embeddings in triage v1** | Free lexical first; measure where it fails |
| **A bespoke queue** | `inbox/wip/done` already proven in `repos/agent-tasks/` |
| **Concept maps inside the Collector** | Interpretive artifacts belong downstream. Screenshots preserve exact UI state and belong to capture; concept maps do not |
| **DVD / physical-reader ingestion** | In the diagram, near-zero present utility against real cost |
| **A second fork** | `base_human_learn_sys` already cost every fix twice, and one was missed for six weeks. Add a backend behind an existing switch |
| **A web UI** | Nothing in the stated need requires one |
| **Merging the voice-chat roadmap** | Separate project; shares only the queue mechanism |
| **Renaming the package now** | Accurate but breaks handoff paths, task specs, cron refs. Defer (D7) |

---

## 9. Decisions needed (do not guess)

| # | Decision | Why | Blocks |
|---|---|---|---|
| **D1** | **Where is the corpus, and can this machine reach it?** (a) sync a subset to the mini, (b) run the indexer on the laptop, (c) shared location | Triage is the highest-value package and is **unusable** from here if the corpus is laptop-only | WP-04, WP-05 |
| **D2** | **What fraction of the epubs are DRM-free?** | Same no-circumvention rule as Udemy. If most are DRM'd, WP-04/05 shrink and priority changes | WP-02, WP-04 |
| **D3** | **Obsidian: reachable? read-only or read/write?** | The glossary source pass is designed around it | WP-11 |
| **D4** | **Where does corpus data live?** Proposal: `CORPUS_INDEX` defaulting **outside the repo** (`~/.local/share/collector/corpus.db`), gitignored — **extracted book text is copyrighted and must not enter git** | Data location + legal hygiene | WP-04 |
| **D5** | **Spend policy for book-length input.** Chunked map-reduce over a 400-page PDF may exceed a whole playlist. Does `ON_EXHAUSTION` stay `wait` for documents? | Changes what gets paid for — the current default never spends unasked; do not change silently | WP-02 |
| **D6** | **Embeddings: local, metered, or not yet?** Recommendation: **not yet** | Adds a dependency and possibly recurring spend | WP-05 v2 |
| **D7** | **Rename to `collector`?** Recommendation: keep the name, call the *system* "the Collector" in docs | Breaks handoff docs, task specs, cron paths | cosmetic |
| **D8** | **One CLI with flags, or separate entry points?** Recommendation: one Typer app, distinct commands (`process`, `index`, `plan`, `read`, `learn`) | Research/summary is its own component with a human caller | WP-01 on |

## 10. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| yt-dlp staleness | High, recurring | WP-12; bump opportunistically (ADR-042) |
| **Unit tests pass while everything is broken** — 136/136 passed while every download 403'd | High | Every new extractor gets a **real-file fixture test**; the integration suite is the only real gate |
| WP-01 touches summarizer, main, transcriber, visuals and ~145 tests | Medium | Keep `TranscriptResult` as a media-flavoured `SourceText`; widen the signature rather than rewriting callers |
| A bare `uv sync` strips extras | Medium, recurring | Adding `[documents]` makes it worse — update the readme in the same commit |
| Chunking degrades cross-chapter connection | Medium | Accept for v1; the reading plan means usually summarizing *selected sections*, not whole books |
| Triage produces a confident but wrong plan | Medium | The "considered and skipped" table makes the ranking auditable. Acceptance is his judgement on a real query |
| Two machines, two data locations | **High for WP-04/05** | **D1 — resolve first** |

## 11. Vocabulary (flagged for the Obsidian pass)

| Term | Discipline | Source |
|---|---|---|
| Scaffolding theory | Bruner | — |
| Spiral curriculum | Bruner | — |
| Conceptual vs procedural knowledge | instructional design | Hiebert & Lefevre, 1986 |
| Surface vs deep processing | cognitive psychology | Craik & Lockhart, 1972 |
| Worked example → fading | cognitive load theory | — |

**Cognitive psychology / instructional design, not UX** (corrected in-session).

## 12. Still unread

| Source | Status |
|---|---|
| Claude chat `human training loop discussion in car` | Not read — tracer bullets, walking skeletons, harness engineering |
| Claude chat `PDF vs EPUB for learning with AI context` | **Not read, directly relevant to WP-02 — read before starting it** |
| `Appearance and aesthetics` msgs 1–65 | Not read; phase plan known only via the handoff summary |
| Obsidian learning notes | Laptop; unreachable |
