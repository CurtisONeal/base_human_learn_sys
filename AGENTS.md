# AGENTS.md — video2notes-v2

Read before changing anything here. The failure mode in this repo is not a
crash — it is **output that is confident, plausible, and wrong**: a healthy word
count, a provenance string claiming real structure, and an exit code of 0.
`FIXED_BUGS.md` has 30 entries and most share that shape.

---

## Two rules that come from repeated failure

Both were learned the expensive way, twice each. They are in the operator's
long-term memory and they apply to any change here.

### 1. Verify the artifact, not the diff

A run that exits 0 proves nothing. **Grep the output for the specific property
you changed**, and do it on the smallest sample that can show it before paying
for a full run.

Measured: a 25-minute pipeline completed cleanly *twice* while producing output
the fix had not touched.

```
code blocks    140 fed to the model  ->    0 in the notes
locators       249 distinct values   ->  239 bare [ch. N], 0 section-level
```

A single-chunk probe cost **~1 minute** and gave a definitive answer. Skipping
it cost 25 minutes and a wrong claim to the user.

**Report before/after numbers.** If you cannot produce them, you have not
verified it.

### 2. Test what the model actually receives

When a change is meant to alter what a model sees, **assert on the rendered
prompt**, not the data structure behind it.

The recurring failure has one shape: *the data improved and the output did not,
because the improvement never reached the prompt.* `Section.locator` went from
15 to 249 distinct values while `to_prompt_text()` still printed one locator per
passage — so the model cited that one, faithfully, 239 times.

```python
rendered = chunk.to_prompt_text()
for section in chunk.sections:
    assert f"[{section.locator}]" in rendered
```

Normalise whitespace when asserting on prompt *text* — prompts are hard-wrapped,
so phrases are not contiguous: `" ".join(PROMPT.split()).lower()`.

A static test proves the text reaches the prompt. It does **not** prove the
model uses it. That needs one real call.

---

## The governing principle

**Free signals gate expensive ones.** Every stage is built this way:

| Free signal | Gates |
|---|---|
| Manual captions exist? | Whisper |
| OCR yield on a frame | the vision model |
| Publisher's TOC | reading the book at all |
| Lexical + vector search | the cross-encoder reranker |

If you add a stage that spends money or CPU, put a free check in front of it.

---

## Non-negotiables

- **Never use YouTube auto-captions.** Manual tracks only. Machine ASR that
  claims to be human-authored corrupts provenance.
- **Never fabricate a locator.** An EPUB has no pages, so it never prints one.
  `structure_is_inferred` must be set whenever structure was guessed.
- **Never let exhaustion escalate to metered.** `SubscriptionExhausted` is a
  distinct exception for exactly this reason.
- **Never write book text into a derived artifact.** Copyright (design decision
  D4). The catalog stores headings and counts, never bodies.
- **Never OCR or de-DRM.** Detect, refuse by name, move on.
- **`claude -p` is an agent, not a completion endpoint.** It runs with
  `--disallowedTools` and a scratch cwd because transcripts are untrusted text.

---

## Before you commit

```bash
uv sync --extra ocr --extra dev --extra documents --extra semantic
.venv/bin/python -m pytest tests/ -q -m "not integration"   # ~5s, 271 tests
.venv/bin/ruff check src/ tests/
```

A bare `uv sync` silently strips every extra. The full suite including
integration takes ~5 minutes; if it takes 40, something is loading a large
model it should not be — check `FW_MODEL` handling.

**Add a `FIXED_BUGS.md` entry for anything that was silently wrong.** State the
numbers, and say plainly when a fix has no test.

**Update `readme.md` after any session that changes behaviour, not just setup.**
