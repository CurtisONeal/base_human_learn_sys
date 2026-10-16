# Long-form notes: the Course Map

You are given per-chapter notes from a long recording. Produce ONLY a course
map for the chapters given — not a summary of the notes, and not a rewrite of
them (the chapter notes are kept in full elsewhere).

Output exactly two things and nothing else:

1. A markdown table with these columns, one row per chapter given, in order:

| # | chapter | time range | gist |
|---|---|---|---|
| 1 | Introduction | [0:00:47–0:10:17] | One line: what this chapter teaches. |

   - `#`, `chapter` and `time range` are given to you with each chapter;
     copy them exactly.
   - `gist` is ONE line (at most ~25 words) saying what the chapter teaches.

2. A heading `### Cross-chapter themes` followed by 3–8 bullets naming ideas
   that recur across the chapters given (cite the chapter numbers, e.g.
   "(ch. 2, 5)").

   - A theme names a TOPIC, never a verdict or conclusion about it: write
     "Testing permission rules with live demos (ch. 6, 7)", not "Permission
     rules don't work as documented" or "Settings need a restart". Outcomes
     belong in the chapter notes, where they are cited and may be superseded.

Rules: work only from the notes given; add nothing that is not in them. No
preamble, no closing remarks, no questions. The first line of your reply is the
table header.
