# Handoff: Research, Learn A Thing, Teach It To Me

Created: 2026-08-23  
Repo location: `/Users/s_admin/Documents/agent_set_up/repos/video2notes-v2`  
Audience: a Claude Code or Codex session resuming local work with access to the `video2notes-v2` checkout.

## Purpose

This handoff preserves the separate project thread about turning source material into a human learning loop.

Do not conflate this with the voice-chat-to-program-design workflow. This project is about ingesting source material, organizing it into learnable structure, supporting practice and retrieval, and preparing a teach-back path.

## Project Boundary

This thread is about:

- Researching or ingesting a thing.
- Capturing source material from videos, transcripts, captions, frames, web pages, PDFs, EPUBs, or notes.
- Turning that material into structured learning notes.
- Adding vocabulary, learning-science scaffolding, practice, retrieval, spaced review, transfer, and teach-back support.
- Using `video2notes-v2` as a source-gathering stage.
- Using `with-learning-science` as the downstream pedagogy/notes stage.

This thread is not about:

- Turning voice chat into programming requirements.
- Posting requirements to GitHub for Hermes.
- Building the agent handoff system, except where it supports the learning pipeline.

## Relevant Local Artifacts

| Artifact | Path | Role |
| --- | --- | --- |
| Video pipeline repo | `/Users/s_admin/Documents/agent_set_up/repos/video2notes-v2` | Current source-gathering and note-generation pipeline. |
| Installed learning skill | `/Users/s_admin/.codex/skills/with-learning-science` | Downstream learning-science and teach-back instructions. |
| Earlier installed template skill | `/Users/s_admin/.codex/skills/learning-template` | Predecessor skill for Markdown class notes. |
| Human-visible skill handoff | `/Users/s_admin/Documents/Codex/2026-08-03/documents-plugin-documents-openai-primary-runtime/outputs/with-learning-science-handoff.md` | Explains the skill and how it relates to `base_human_learn_sys`. |
| Current prior run outputs | `/Users/s_admin/Documents/agent_set_up/repos/video2notes-v2/previous_run_results` | Source examples with transcripts, summaries, frames, and WAV files. |

## Relevant Cloud Sessions

These sessions were visible through the signed-in Claude cloud UI in Chrome.

| Session | URL | Why It Matters |
| --- | --- | --- |
| `Appearance and aesthetics` | `https://claude.ai/chat/00245818-8b05-47ba-a827-3153ba99dbed` | Main design thread for the human learning loop. Messages 40-65 define the phase breakdown and the phase 2.5 pedagogical scaffolding layer. |
| `human training loop discussion in car` | `https://claude.ai/chat/1b94aaf5-9a68-48d7-aaa0-67b1f9f820b3` | Connects tracer bullets, walking skeletons, and harness engineering to the video-to-notes work. |
| `Fix video fragment sorting and context management` | `https://claude.ai/code/session_01BS2oPoZytacDJNV6jRDuTc` | Freshest cloud-side Claude Code context found. It explicitly ties `video2notes-v2` to `with-learning-science` and identifies the architecture boundary. |

## Current Design Understanding

`video2notes-v2` should be treated as the source-gathering layer, not as the whole learning system.

It currently does or is designed to do:

- Probe a source without downloading audio.
- Prefer human-authored captions when they are usable.
- Fall back to Whisper when captions are unavailable or bad.
- Extract keyframes when enabled.
- Use free OCR before escalating selected frames to a vision model.
- Archive outputs under `previous_run_results`.

`with-learning-science` should be treated as the downstream learning layer.

It defines learning jobs:

| Job | Purpose |
| --- | --- |
| `reference` | Fast lookup, commands, vocabulary, source traceability. |
| `study` | Retention, retrieval practice, spaced review, misconception checks. |
| `implementation` | Setup, code, verification, troubleshooting, transfer to work. |
| `teach-back` | Explanation ladder, demo plan, learner checks, re-teach moves. |

## Phase Breakdown From The Voice Design Chat

The `Appearance and aesthetics` session contains the clearest phase plan.

### Phase 1: Separate Capture From Interpretation

Split transcription/capture from interpretation.

The reusable part should read raw source text or transcript fragments and expose them to later stages. It should not be locked to YouTube video only. Later modalities such as books, PDFs, EPUBs, web pages, and course pages should be able to plug into the same interpretation layer.

### Phase 2: Initial Learning Output Without GUI/Keyframes

Get the first learning output working from text alone.

This means real notes and concept extraction end to end, without depending on screenshots, GUI walkthroughs, or visual extraction. The goal is to prove the text-to-learning path before expanding modality support.

### Phase 2.5: Pedagogical Scaffolding Layer

Add a layer between raw concept extraction and hands-on practice.

The layer should not just summarize. It should reorganize extracted concepts into a learnable hierarchy:

- Big-picture shelf: what this is, why it exists, and where it fits.
- Bare-bones core concepts: what must be understood first.
- Deeper mechanics: what makes the thing work under the hood.

This is where the learning-science lens belongs. The cloud session associated it with Bruner's scaffolding theory, spiral curriculum, and instructional-design/cognitive-psychology distinctions such as surface vs deep processing and conceptual vs procedural knowledge.

Parked backlog item: later adaptation to what Curtis already knows. First pass should be a fixed scaffold, not learner-model personalization.

### Phase 3: Testing And Kata-Style Practice

Build a small end-to-end practice project where the learner applies the concept.

This should be concrete enough to verify learning by doing, not just reading. It should support a worked-example-to-fading-practice path: full example, partial example, independent task.

### Parked Later Work

Later, not first:

- Keyframes and GUI-heavy visual layer.
- EPUB/PDF/web text ingestion.
- Timed recall quiz engine.
- Aikido or other domain-specific vocabulary tracks.
- Learner-state model that adapts to what Curtis already knows.

## Fresh Claude Code Finding

The newest cloud-side Claude Code session, `Fix video fragment sorting and context management`, found this architecture boundary:

- `video2notes-v2` is not competing with `with-learning-science`.
- `video2notes-v2` is the source-gathering stage the skill assumes exists.
- `with-learning-science` owns the learning taxonomy and downstream note structure.
- The seam is likely a domain/profile handoff where source-gathering output carries enough metadata for the skill to choose `reference`, `study`, `implementation`, or `teach-back`.

The same session compared actual visual output against the skill's screenshot checklist and found a gap:

| Checklist Item | Current Concern |
| --- | --- |
| Teaches what text or Mermaid would not | Some frames are mostly text and may not need screenshots. |
| Excludes irrelevant chrome | Full frames often include browser or OS furniture. |
| Relative path, alt text, caption | Path and alt text exist; caption may be missing. |
| Says what to inspect | Current VLM output describes what is present but does not direct learner attention. |
| Not decorative | Current gating helps, but output should better justify why the frame matters. |

Immediate small action proposed there:

- Update `VLM_PROMPT` in `src/video2mdnotes/core/visuals.py` to include a short caption and an explicit "what to inspect" direction.

Open design decision:

- Keep Mermaid/concept-map generation downstream in `with-learning-science`, not in `video2notes-v2`, unless future evidence says source-gathering must own it.

Reasoning:

- Screenshots preserve exact UI state and belong naturally to the video pipeline.
- Concept maps are interpretive teaching artifacts and belong naturally to the learning skill.

## Suggested Next Claude Code Prompt

```text
Resume the Research/Learn A Thing/Teach It To Me thread. Do not work on the voice-chat-to-program-design handoff system. Read handoffs/2026-08-23-research-learn-teach-handoff.md, then inspect src/video2mdnotes/core/visuals.py and ~/.codex/skills/with-learning-science/references/visuals.md. Propose the smallest tested change that makes VLM visual notes include a caption and a clear "what to inspect" learner-attention cue, while keeping Mermaid/concept-map generation downstream in the learning skill.
```

## How The Context Was Retrieved

This was not actual mind reading. The context came from browser-accessible cloud history and local files.

Tools and surfaces used:

- `browser:control-in-app-browser` skill was read to choose the right browser-control surface.
- `chrome:control-chrome` skill was read because the task depended on signed-in browser state.
- `plugin-management` skill and `tool_search` were used to check whether a direct ChatGPT/cloud-history connector existed. No direct ChatGPT history connector was exposed.
- `mcp__node_repl.js` controlled the browser runtime.
- Browser APIs used included:
  - `agent.browsers.list()`
  - `agent.browsers.getForUrl("https://chatgpt.com/")`
  - `browser.user.openTabs()`
  - `browser.user.history(...)`
  - `browser.tabs.new()`
  - `tab.goto(...)`
  - `tab.playwright.domSnapshot()`
  - Claude's visible search dialog through Playwright locators.

Observed browser/account state:

- Two Chrome profiles were available.
- Both profiles opened ChatGPT logged out, so ChatGPT cloud history was not accessible.
- The second Chrome profile had a signed-in Claude session.
- Claude's sidebar and search UI exposed cloud chats and Claude Code sessions.
- Searches included `with learning science`, `human training loop`, `human learning`, `learning science`, `base_human_learn_sys`, `video2notes`, and `learning skill`.

Relevant search results:

- `human training loop` surfaced `human training loop discussion in car`.
- `human learning` surfaced `Appearance and aesthetics`, with a snippet mentioning "my human learning loop platform."
- `video2notes` surfaced `Fix video fragment sorting and context management`, a Claude Code session marked as the freshest visible result.

Safety boundary:

- I did not inspect cookies, local storage, passwords, or session stores.
- I did not send messages, submit forms, or modify the cloud sessions.
- I read visible page state and search results only.

## Cautions

- The Claude URLs require the signed-in account that owns those conversations.
- The visible Claude chat may load only slices of long conversations. The phase plan above is from visible messages 40-65 in `Appearance and aesthetics`.
- Voice transcript text contains recognition errors, so preserve intent but verify exact names before implementation.
- Do not treat `base_human_learn_sys` as the current full learning system. The current implementation path is `video2notes-v2` plus the installed `with-learning-science` skill.

