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
artifacts**, not a pipeline stage. The learning layer reads notes; it does not reach
back into extraction.

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
