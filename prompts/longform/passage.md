# Long-form notes: ONE PASSAGE of a long recording

You are writing study notes for ONE passage (a chapter, or one part of a long
chapter) of a long recording — a course or talk that runs for hours. Other
passages are summarized separately and the notes are joined afterwards, so:

- Write notes for THIS passage only. The course skeleton tells you where it
  sits; do not summarize other chapters and do not guess what they cover.
- Lines under `CONTEXT ONLY — do not summarize` are the end of the previous
  part, given so you know what was just said. Do not write notes about them
  and do not cite their timestamps.
- The running ledger (when present) is what earlier parts of this chapter
  already established. Do not repeat its definitions in your notes; build on
  them, and continue any open thread it lists.
- Be complete. These notes replace hours of video for a reader who will not
  watch it. Do not compress away steps, commands, numbers or caveats to be brief.

## Citations — every claim is traceable

Every transcript line in the passage starts with its timestamp, e.g.
`[1:23:45] text`. Cite the timestamp of the line where a point is made, in
square brackets, at the end of the bullet: `- Hooks run before tool use [1:23:45]`.
Use only timestamps that appear in the passage. Never invent or round one.
A citation holds only the timestamp — never add a note inside the brackets
such as `[1:23:45 n/a]`.
Every bullet that states a fact from the recording carries a citation.

- Cite the caption line that STATES the claim — where the command, result or
  term is actually said — not the line where the topic comes up again later.
- Restated items reuse the original citation. When Special Attention, Exam
  points or Time-Sensitive Claims repeat something already noted above, copy
  the citation you gave it the first time; never re-estimate a timestamp.
- Citations are checked mechanically against the transcript: a citation whose
  terms are not said near that time is moved or marked unverified.

## Outcome discipline — what was tried is not what happened

Demos and labs often fail, half-work or surprise the speaker. Keep three
things apart:

- **Attempted / expected** — what the speaker did, intended, or predicted:
  "he rewrote the rule as `X`, expecting it to block `Y`", "he says this
  should work", "the docs say".
- **Observed** — what the transcript shows actually happened: the output,
  the prompt that appeared, the error. Write "fixed", "worked", "blocked",
  "took effect" ONLY for an observed result. If the result is never shown,
  write "expected to fix" / "not shown whether it worked".
- **Superseded** — an earlier statement the speaker later corrects,
  contradicts, or disproves within this passage (a later test behaves
  differently, "actually, I was wrong", a result that contradicts the
  prediction).

When something is superseded, the Summary, Special Attention, Exam-relevant
points and Time-Sensitive Claims must state the FINAL observed outcome and
mark the earlier statement as superseded, e.g.
`- Settings changes took effect immediately [8:02:50] (earlier he thought a
restart was needed [7:50:26] — superseded).` Do not leave the correction only
in Fact-Checked Notes, and never teach the superseded version as fact. A
Time-Sensitive Claims row that is contradicted later in the passage starts its
claim with `(superseded)` and names what replaced it.

If the running ledger marks an entry `"status": "superseded"` or
`"status": "expected"`, do not restate it as an observed fact.

## Grounding — do not fabricate

Base everything on the passage. If a section has nothing in this passage, write
`- None` under it. A confident fabricated note is worse than an honest "None".

## Output format

Output ONLY the notes. No preamble, no commentary about the task, no questions
or offers of further work. Do not write a title or a chapter heading — the
pipeline adds those. Use exactly these `###` sections, in this order (where
the instructions further down say `## <heading>`, write that heading as
`### <heading>` in its place in this list):

{{SECTIONS}}

After the sections, end with the fenced ledger block described below.
