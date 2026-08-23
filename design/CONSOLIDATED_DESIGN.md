# Consolidated design: research → learn → teach

Created 2026-08-23. Single place for the design threads that were scattered across
Claude chat (cloud-only), Codex handoffs, an installed skill, and this repo.

**Status of sources**

| Source | Reachable | Captured here |
| --- | --- | --- |
| `handoffs/2026-08-23-research-learn-teach-handoff.md` | local | ✅ fully |
| `handoffs/2026-08-23-voice-chat-to-program-design-handoff.md` | local | ✅ (separate thread — see boundary) |
| `~/.codex/skills/with-learning-science/` | local | ✅ taxonomy + visual checklist |
| Claude chat `Appearance and aesthetics` (Jul 26) | cloud, signed-in browser | ⚠️ **tail only** — "Load earlier messages" would not advance; phases 1–3 are known via the handoff's summary, not verbatim |
| Claude chat `human training loop discussion in car` (Jul 30) | cloud | ❌ not yet read |
| Claude chat `PDF vs EPUB for learning with AI context` (Aug 10) | cloud | ❌ not yet read — **not cited in either handoff**, and directly relevant to Phase 1 modality work |
| Obsidian learning notes | laptop | ❌ Curtis adding |

---

## The Collector (from the hand-drawn diagram, 2026-01-14)

Source: `__assets/images/everything2mdnotes_video2notes_20260114_media_to_mdnotes_Page_1.png`
in the Obsidian vault (laptop). Referenced as `everything2mdnotes`.

**This is the clearest statement of the capture component, and it predates the phase
plan by six months.** It shows two source families converging on one text pipeline:

```
MEDIA FAMILY                                    needs: audio -> transcript
  youtube ─────────────┐
  paywall videos ──────┤ = "streaming bits"
  meeting audio ───────┤ -> .ast
  DVD video ───────────┼─> physical reader -> folder structure -> file transfer
                       │   (also: "streaming?")
  audiobook? ──────────┘ -> "file type?"
                             │
                             v
        downloads audio on all playlists -> transcribes off-machine LLM -> SUMMARIZES

DOCUMENT FAMILY                                 needs: text extraction only
  epub ────┬─> "in web" / "on comp" ─┐
  pdf ─────┤                          ├─> extractor -> TEXT EXTRACTOR
  docx ────┤                          │
  md ──────┘                          │
  html ──> BeautifulSoup -> cleanup ──┘
```

### What this settles

1. **The component is "the Collector", not "video2notes".** Video is one input among
   eight-plus. The name `everything2mdnotes` is the intent; `video2mdnotes` was the first
   vertical slice.
2. **Two families, not one pipeline.** Media needs an *audio→transcript* stage that
   documents skip entirely. Both then converge on the same summarizer. That convergence
   point is precisely the Phase 1 seam.
3. **Sources in the diagram that appear in no handoff and no code:** DVD (with a *physical
   reader* step and folder-structure handling), meeting audio, audiobooks, docx, html.
   Paywall video is drawn as "streaming bits" — the Udemy problem, acknowledged in the
   original design rather than discovered later.
4. **"Transcribes off machine LLM"** — the diagram already assumed transcription might be
   remote. Current implementation is local faster-whisper, which is a *stricter* choice
   (free, offline) and worth keeping, but the design anticipated both.

### Diagram vs. what exists today

| Diagram element | Status |
| --- | --- |
| youtube -> audio -> transcript -> summary | ✅ built |
| playlists | ✅ built |
| paywall / streaming bits | ⚠️ partial — page-embed scrape reaches public embeds only; login-gated is deliberately not circumvented |
| pdf / epub / docx / md / html -> text extractor | ❌ none built. **This is the Phase 1 gap.** |
| meeting audio, audiobook, DVD | ❌ none built |
| BeautifulSoup + cleanup for html | ❌ not built (embed scraping is regex over raw HTML, not content extraction) |

The document family is the cheaper half — no download, no transcription, no visual layer —
and it is entirely unbuilt. That is a strong argument for it as the MVP slice: it proves
the source-agnostic seam with the least machinery.

## The classification that matters

The organising question is not "what features do we want" but **what is a component
with its own purpose, and what is a plugin to one.** A plugin cannot justify its own
existence; a component can be used alone and still be worth using.

### Components (own purpose, usable alone)

| Component | Purpose on its own | Current state |
| --- | --- | --- |
| **Source capture** | Get material out of a source into text + provenance. Useful even if nobody ever learns from it. | Built for video (captions-first, Whisper). Locked to yt-dlp shapes. |
| **Research / collection + summary** | **Its own purpose.** "What is this, what did it say, is it worth my time." Ends at a good summary. Does not owe anyone a learning loop. | Built and working — this is what `video2notes-v2` does today. |
| **Learning organisation** | Turn material into a learnable structure: shelf overview → core concepts → deeper mechanics. | Not built. `with-learning-science` defines it; nothing implements it. |
| **Practice / retention** | Retrieval practice, spaced review, kata. Makes it stick. | Not built. |
| **Teach-back** | Explanation ladder, demo plan, learner checks. | Not built. Defined in the skill. |

### Plugins (enhance a component, cannot stand alone)

| Plugin | Attaches to | Why it is not a component |
| --- | --- | --- |
| Visual extraction (keyframes, OCR, VLM) | Source capture | Screenshots with no notes around them teach nothing. |
| Frame ranking / blank-frame guards | Visual extraction | A plugin to a plugin. |
| Domain prompt selection | Research/summary | Changes *how* a summary is written; produces nothing alone. |
| Visual profiles | Visual extraction | Tuning. |
| Captions-first | Source capture | An optimisation of capture, not a capability. |
| Glossary / vocabulary extraction | **Both** research/summary *and* learning organisation | Useful in a plain summary ("what do these words mean") and required by the learning loop. The clearest shared plugin. |
| Concept maps (Mermaid) | Learning organisation | Interpretive; belongs downstream. |

### The insight that reorders the roadmap

**Research/collection with a summary is its own purpose, not merely stage one of
learning.** Two consequences:

1. It must remain usable without any learning stage attached. Today it is; keep it that way.
2. It can *also* be the first move of a learning spike. Same output, two consumers.

That means the seam between "capture + summarise" and "learn" is a **handoff of
artifacts**, not a pipeline stage.

**But the flow is a loop, not a line.** An earlier version of this document claimed the
learning layer "never reaches back into extraction." That was wrong, and it conflated two
different things:

| | Correct? |
| --- | --- |
| Learning must not depend on extraction *internals* (yt-dlp shapes, frame paths, OCR) | ✅ yes — that is the decoupling worth keeping |
| Learning must not *cause* more research | ❌ **no** — teaching is precisely where gaps surface |

Trying to teach something is the activity that exposes what a source never covered. A
teach-back stage that hits "I cannot explain this because the notes do not say" has
discovered a real research need, and swallowing that is a design failure.

**The resolution keeps both properties: the learning layer does not *call* extraction, it
*emits a research request* that the Collector consumes as an ordinary input.** A new job in
the queue, not a reach-back through the stack. Decoupling preserved, feedback enabled.

```
Collector -> notes -> Learning/Teach-back
    ^                      |
    |                      v
    +---- research request (a new artifact, queued like any source URL)
```

Note this is the **same shape as the voice-chat-to-program-design thread**: a durable
artifact lands in a queue that a worker polls. Gap-flags are that pattern applied to
research rather than to code. The two threads stay separate in purpose but share this
mechanism — worth knowing before either builds its own bespoke queue.

### Gap kinds worth distinguishing

Not every gap should spawn the same thing:

| Gap | Surfaces during | Right response |
| --- | --- | --- |
| **Coverage** — the source never addressed it | notes quality check | research request: find a source that does |
| **Comprehension** — notes cover it but cannot be explained from them | teach-back | research request: find a *better/different* source |
| **Best practice** — the source teaches something outdated or contradicted | notes quality check, or a later source disagreeing | **flag, do not silently resolve** — a human decides which is right |
| **Vocabulary** — a term is used but never defined | glossary extraction | look it up; check Obsidian first (the source pass) |
| **Prerequisite** — depends on something not yet learned | scaffolding (Phase 2.5) | order the material, or spawn a prerequisite spike |

The best-practice case is the one Curtis raised and the one that most needs care: two
sources disagreeing is *information*, not an error to auto-resolve. The system should
surface the conflict with both citations, not pick a winner — the same discipline as
ADR-035's rule to write `- None` rather than invent, and ADR-037's insistence that
verified and unverified claims stay distinguishable.

This also means **research/collection has two entry points**, not one: a human saying "go
look at this", and a learning stage saying "we need this". Same component, same output,
two callers — which is further evidence it is a component rather than a stage.

---

## Phase plan (from `Appearance and aesthetics`, via the handoff summary)

| Phase | Content | Against current state |
| --- | --- | --- |
| **1. Separate capture from interpretation** | Interpretation reads raw text/transcript fragments. Not locked to YouTube. Books, PDFs, EPUBs, web pages plug into the same layer. | **Not started.** `TranscriptResult` is produced only by two YouTube-shaped paths and consumed directly by `generate_summary`. Precondition partly met: the transcript is now written before summarising, so it survives independently. |
| **2. Learning output from text alone** | Real notes and concept extraction end to end, **without** screenshots, GUI walkthroughs or visual extraction. | **Not started.** |
| **2.5 Pedagogical scaffolding** | Reorganise concepts into a hierarchy: shelf overview → bare-bones core → deeper mechanics. Bruner's scaffolding, spiral curriculum, surface vs deep, conceptual vs procedural. | **Not started.** The `Domain` axis built on 2026-08-22 is shaped for this — prompt selection by subject is the mechanism this phase needs. |
| **3. Kata-style practice** | Small end-to-end project. Worked example → partial → independent. | **Not started.** |

**Parked by the plan:** keyframes and the GUI-heavy visual layer; EPUB/PDF/web ingestion;
timed recall quiz engine; domain-specific vocabulary tracks; learner-state model.

> ⚠️ **Sequencing note.** The visual layer (keyframes, OCR, vision tier, ranking, guards)
> was built on 2026-08-16→22 and is tested and working — but the plan parks it until after
> Phase 2. It is not wasted (it rescued a silent video from a 94-byte placeholder to real
> notes) but it ran ahead of the intended order. Phases 1 and 2 remain unstarted.

---

## Vocabulary / glossary thread

From the tail of `Appearance and aesthetics`. Curtis asked that terms be flagged with a
marker for a later **source pass against the Obsidian learning notes**, to separate
"already in my notes" from "new reference to look up".

| Term | Discipline | Footnote |
| --- | --- | --- |
| Scaffolding theory | Bruner | — |
| Spiral curriculum | Bruner | — |
| Conceptual vs procedural knowledge | instructional design | Hiebert & Lefevre, 1986, *Conceptual and Procedural Knowledge: The Case of Mathematics* |
| Surface vs deep processing | cognitive psychology (levels of processing) | Craik & Lockhart, 1972, *Levels of Processing: A Framework for Memory Research* |

Explicitly corrected in-session: this framing is **cognitive psychology / instructional
design, not UX.**

This is the strongest evidence that **glossary is a real component-level concern**, not a
nicety — it is the atom both a summary and a retention loop operate on, and it already has
a source-of-truth (Obsidian) that neither system currently reads.

---

## Boundary: what this is NOT

The `voice-chat-to-program-design` thread is a **separate project**: voice → clarifying
conversation → requirements document → GitHub issue → Hermes/worker → report or PR. It
shares the "durable artifact handed to another agent" pattern and nothing else. Do not
merge the two roadmaps.

---

## MVP candidate

Given the classification above, the smallest thing that is *whole*:

1. **Phase 1 seam** — a source-agnostic input type (text + optional timed segments +
   provenance) that `TranscriptResult` already satisfies, so a PDF/EPUB/web reader can feed
   the same summariser without touching yt-dlp. Mostly a type and an adapter.
2. **Glossary extraction** as a plugin to research/summary — new terms, defined, flagged
   for the Obsidian source pass. Independently useful *today* for plain research summaries,
   and a Phase 2.5 prerequisite.
3. **Phase 2 on text only** — learning organisation over an existing transcript, no visuals.

Deliberately excluded from MVP: more visual work, quiz engines, learner modelling,
concept maps, multi-summary evaluation.

---

## Open, needs Curtis

- Obsidian: which vault/paths hold the learning notes, and should the glossary read them
  (source pass) or only write to them?
- Three cloud sessions still unread — `human training loop discussion in car`,
  `PDF vs EPUB for learning with AI context`, and messages 1–65 of `Appearance and
  aesthetics`.
- Does the research/summary component get its own entry point separate from the learning
  path, or is one CLI with flags enough?
