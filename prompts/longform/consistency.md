# Long-form notes: cross-section consistency check

The sections of one long recording (talks of a meetup, chapters of a course)
were summarized INDEPENDENTLY, so a later section can contradict an earlier
one. Example: section 1 says "the giveaway was not drawn at the event; the
winner will be announced Wednesday", but section 6 shows the winner being
drawn at the event. The later section records what finally happened.

You are given a compact list of statements extracted from the notes — events,
outcomes, plans, numbers and named people — each with a reference id such as
`S1.4` (section 1, statement 4), the section it comes from and its citation.
Sections are listed in recording order.

Find every pair where a statement in a LATER section contradicts, corrects or
supersedes a statement in an EARLIER section:

- an outcome or plan that later turned out differently ("will be announced
  Wednesday" vs "the winner was drawn tonight");
- a number, percentage or figure that disagrees with the figure given for the
  same thing elsewhere — including a speaker QUOTING another speaker's figure
  ("the story Morgan referenced was like 95%") when that speaker's own section
  gives a different figure (80%);
- a fact about a named person (role, employer, what they said) that a later
  section states differently.

Rules:

- Only real contradictions about the SAME thing. Two different numbers about
  different things are not a contradiction. A later statement that merely adds
  detail is not one either.
- When one later statement contradicts several earlier statements (the same
  plan restated in a summary, a warning and a time-sensitive row), list EACH
  earlier statement as its own pair.
- `kind` is `"superseded"` ONLY when the later statement REPLACES the
  earlier one as fact — the earlier statement is now simply false (a plan
  that was cancelled, an outcome that turned out differently: "will be
  announced Wednesday" vs "the winner was drawn tonight").
- `kind` is `"partly contradicted"` when the later section shows the earlier
  statement was only partly true or had exceptions, but it is not replaced
  (e.g. "setup is not covered in this course" vs a later quick install
  demo).
- `kind` is `"discrepancy"` when two figures or attributions disagree and the
  notes cannot tell which is right (e.g. a quoted figure that differs from
  the original speaker's). When unsure, use `"discrepancy"`, never
  `"superseded"`.
- `resolution` is ONE short sentence saying what the later section shows,
  using only the statements given. Use the words of the specific clause that
  conflicts (the notes point readers at that clause's citation).
- Use only the reference ids given. Never invent a statement.

Reply with ONLY a JSON array and nothing else (an empty array `[]` when there
are no contradictions), for example:

[{"earlier_ref": "S1.3", "later_ref": "S6.2", "kind": "superseded",
  "resolution": "The winner was drawn at the event from 112 submissions."}]
