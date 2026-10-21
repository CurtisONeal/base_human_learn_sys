# Span digests (week / month) — design

Command: `digest --collection <dir|subject> --by week|month [--since] [--until] [--out]`.
Code: `core/digest.py`. Prompts: `prompts/digest/span.md` and `reduce.md`. Tests:
`tests/test_digest.py`.

## Question

Can a summarizer run over a span of episodes? For example, take the last 30 days of
The AI Daily Brief and summarize them by week or as a whole month, without
re-transcribing anything.

## Input

The input is the per-episode NOTES, never transcripts:

- a package's `notes.md`, or a run dir's `summaries/*.summary.md`
- plus the episode's `source_meta.json`

From each episode the tool reads:

- frontmatter `published` / `as_of`, `source` and `duration_s`
- every `### Summary` and `### Special Attention`
- the single `## People` table (`people.parse_rows`)
- Key Concepts bullets
- the Time-Sensitive Claims table (`notes_format.parse_time_sensitive`)

Package notes carry YouTube links in their citations. They are converted back to plain
`[H:MM:SS]` before the notes go into the prompt.

## Grouping

Episodes are grouped by `published`:

- **Weeks** are ISO weeks, Monday–Sunday, named `YYYY-Www`. A Sunday belongs to the
  week that started the previous Monday.
- **Months** are calendar months, named `YYYY-MM`.

The pipeline stores the publish date as a date (YouTube's upload date), and that date is
used as the America/New_York calendar day. A full timestamp, if a source ever has one,
is converted to America/New_York first.

## Split of work: model vs deterministic

| Part | Who |
|---|---|
| title, overview | model |
| top stories (headline, summary, per-episode segments) | model |
| ranking of top stories: number of episodes, then minutes from segments | deterministic |
| threads (first mention → developments → latest, dated) | model writes; dates come from the episodes |
| People table: one row per person across episodes (`people.merge_people`, ASR sounds-alike merge, most specific role, richest stated background) | deterministic |
| "relevance this span" cell of the People table | model (cited); falls back to the merged per-episode relevance |
| key terms new this span (not defined in any earlier episode of the collection) | deterministic |
| time-sensitive claims table | deterministic |
| which claims a later episode supersedes (pairs of claim ids `C<n>`) | model |
| annotation of superseded claims (`(superseded)` prefix + pointer, consistency.py style) | deterministic |
| what changed vs the previous span's digest (only when one exists) | model |
| open questions | model |
| episode table, all links, the citation check, the link check | deterministic |

## Citations: no uncited claims

The model must cite `E<n> H:MM:SS` for every item. `validate()` then checks each
citation, the same way eval P2 does:

- the label is an episode of the span;
- the time is within 30 s of a timestamp that this episode's notes actually cite;
- the time is inside the recording.

Invalid citations are dropped. An item left without a valid citation is dropped too.
Both are counted in the frontmatter (`citations`, `citations_dropped`, `items_dropped`)
and listed in `## Citation check`.

Valid citations are rendered as `youtu.be/<id>?t=` links. Each story also links to the
episode notes:

- **Run-dir notes** (plain headings): to the notes section that contains the time
  (`#Window 1 …`, built with `obsidian_anchor`).
- **Package notes:** to the file only. Package headings contain a link
  (`## Window 1 [0:00–0:25](https://…)`), and how Obsidian resolves an anchor to such a
  heading is not certain. The first real run caught exactly this: 9 broken anchors.

After rendering, `check_digest_links` checks every link:

- each relative link resolves (the file exists and the `#heading` matches, with the
  package resolver's rules);
- each YouTube link belongs to an episode of this span and falls within its duration.

The result goes in `link_check: pass|FAIL`, and the CLI exits 1 on FAIL.

## Budget and map-reduce

Each episode is packed compactly: summaries, special attention, people, key-term names.
The cap is 16k characters per episode. Claims are listed once, with ids.

If a span's prompt is over `MAX_INPUT_CHARS` (120k):

1. **Map:** the episodes are cut into consecutive sub-spans, and each gets a partial
   digest in the same schema with global `E` labels.
2. **Reduce:** one call merges the partials (`reduce.md`).

## Months are hierarchical (the choice)

A month is mapped over its ISO weeks, each clipped to the month:

- If a week digest already exists and its episode set equals the clipped week, its
  cached JSON is reused as that week's partial. Its `E` labels are remapped to the
  month's labels, its claim pairs are dropped, and it costs no call.
- Any other week is computed as a partial and cached. This covers boundary weeks that
  straddle two months, and weeks with no digest yet.

One reduce call then writes the month.

Why:

- A month of a daily show (20–30 episodes × about 25k characters of notes) never fits
  one call.
- The week digests usually exist already, so a month costs 1 call instead of 5.
- The week and month views stay consistent.

The deterministic parts are the exception: People, new terms and claims are rebuilt from
the episode notes, so nothing is lost in summarizing a summary.

## Idempotence and caching

Each call is cached in `<out>/.digest_cache/<name>.json`. The cache is keyed by a hash
of the exact system prompt plus user prompt. That prompt contains:

- every episode's packed notes;
- the claims;
- the previous digest's headlines.

So:

- a rerun with the same episodes makes 0 calls and leaves the file byte-identical;
- a new or edited episode changes that span's hash and re-calls only that span.

Spans are built oldest first, so a week's "What changed" sees the previous week's digest
on the same run.

## Partial spans

A span is labelled `partial: true`, with a warning callout, in any of these cases:

- it is not over as of today;
- `--since`/`--until` cut it;
- the collection's `_index.md` (from `playlist-index`) lists more episodes in the span
  than have notes.

## Real smoke (2026-10-21)

`digest --collection ai_daily_brief --by week` was run over the 3 packaged AI Daily
Brief episodes (2026-10-19 and 2026-10-20). They form week `2026-W43`, which is partial.

- **Calls:** 1 opus call (61 s). The rerun made 0 calls and left the file unchanged.
- **Citations:** 128 kept, 0 dropped.
- **Links:** `link_check: pass`.
- **Content:** 8 top stories, of which the Anthropic fund spans 2 episodes. There are 2
  threads, a People table merged across 3 episodes (NLW and Musk appear once each, with
  their episodes listed), and 55 claims, none superseded within the 2 days.
