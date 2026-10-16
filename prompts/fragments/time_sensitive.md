## Time-sensitive claims — date everything that can go stale

Some statements are only true *as of a date*. The source header gives that
date as `Published:`. Your own training data has a different, unrelated date —
never use it for as-of.

Add a section under the heading `## Time-Sensitive Claims`, placed immediately
before `## Fact-Checked Notes`. It is a markdown table with exactly these
columns, in this order:

| claim | timestamp | as-of | type | verify-by |
|---|---|---|---|---|
| <claim as stated> | [H:MM:SS] | <published date, YYYY-MM-DD> | <type> | <official page or document to check> |

Include a row for every claim whose truth depends on when it was said:

- versions (model, SDK, CLI, package or API versions; "latest", "v2")
- features and availability (beta / GA, regions, plans, "you can now ...")
- pricing, quotas, rate limits, context-window or token limits
- exam details: domains, weights, question count, format, duration, passing
  score, cost, retake policy
- news and announcements (launches, deprecations, acquisitions, dates)
- anything said with "currently", "now", "recently", "new", "at the time of
  recording", "as of today", "soon"

Do NOT include personal or ephemeral session state — the speaker's own usage
meter or quota ("I'm at 72%", "33% used", "100% usage"), countdowns ("resets
in 9 minutes", "15 minutes left"), today's outages or glitches ("not working
today"), or what happened to his account during the recording. Such a row
goes stale within hours and teaches nothing. Include it only when it states a
product policy (e.g. "usage resets on a five-hour window", "extra credits are
used once the plan limit is reached") — and then write the policy, not the
meter reading.

Column rules:

- **claim** — one short sentence stating the claim as the speaker made it.
- **timestamp** — the transcript timestamp where it is said, e.g. `[1:23:45]`.
- **as-of** — the source's `Published:` date from the header (YYYY-MM-DD).
  If the header says `Published: unknown`, use the `Captured:` date and write
  it as `YYYY-MM-DD (captured)`. Never leave it blank, and never write
  "today", "recent", or your own knowledge-cutoff date.
- **type** — exactly one of: `version`, `feature`, `pricing`, `exam`, `news`,
  `other`:
  - `version` — a version number, model name/generation, "latest";
  - `feature` — what a product does or allows, including its limits, buffers,
    defaults, enum values and option lists (e.g. "autocompact reserves 22%",
    "`stop_reason` is `tool_use` or `end_turn`", "sessions are kept 30 days");
  - `pricing` — prices, plans, credits, quotas and rate limits;
  - `exam` — exam format, domains, weights, scoring, eligibility;
  - `news` — launches, renames, deprecations, announcements;
  - `other` — only when none of the above fits.
- **verify-by** — a concrete check a reader can do: name the official page or
  document (e.g. "Anthropic pricing page", "Claude Code docs — permissions",
  "CCA-F exam guide PDF"), not "check online".

If there are truly no time-sensitive claims, still write the heading, the
header row and the separator row of the table, then a line `- None` under it.

## Fact-Checked Notes — staleness is relative to the published date

In `## Fact-Checked Notes`, judge whether a claim is outdated relative to the
source's `Published:` date, not relative to today. A claim that was correct on
the published date is not an error in the source even if it has changed since.

When a check rests on your own knowledge (not on something shown in the
recording), say so: write "per model knowledge (cutoff uncertain)". Never write
"correct as of <published date>" — your knowledge cannot tell you what was
true on that date.

State the uncertainty explicitly: model knowledge has a cutoff and cannot see
changes after it. When the published date is after, or close to, that cutoff,
say that the claim cannot be confirmed or refuted from model knowledge and
point to its verify-by check rather than asserting either way.

Write these notes in a neutral, impersonal voice — never first person. Do not
write "my knowledge cutoff", "I can't confirm", "I cannot verify" or "as far
as I know". Write instead, e.g.: "per model knowledge (cutoff uncertain)",
"cannot be confirmed per model knowledge (cutoff uncertain)", "not verifiable
from model knowledge; check <verify-by>".
