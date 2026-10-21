# Sources Referenced + annotate-people (WP-P2)

Code: `core/cited_sources.py`, `core/annotate_people.py`, `core/digest.py`
(Sources of the span), CLI `annotate-people` in `main.py`. Prompts:
`prompts/fragments/sources.md` (media), `_DOC_SOURCES_RULES` in
`core/summarizer.py` (documents), `prompts/annotate/people.md`. Tests:
`tests/test_sources_section.py`, `tests/test_annotate_people.py`.

## Question

Shows like The AI Daily Brief constantly cite outlets, newsletters,
podcasts, papers and posts, and name people by role ("the Secretary of
State"). How do we keep both durable across episodes — who is who, which
sources keep coming up — without inventing facts or re-describing the same
person every week?

## Sources Referenced (per notes file)

| part | who |
|---|---|
| per-passage table (source · type · cited by (role) · cited for · date/edition · citations) | model, prompt variant `current` only (`legacy` byte-identical) |
| one merged `## Sources Referenced` after People: same source = same normalised name (case, leading "the", hedges, trailing type word); citations unioned in time order; a row naming a People-table person is dropped | deterministic |
| citations → YouTube links | `package` (same as every other citation) |
| `## Sources of the week|month` in digests: merged across episodes, ranked by episodes then times cited, episode-notes links + cited YouTube links | deterministic, not in the digest prompt (cache keys unchanged) |

Rules given to the model: outlets/shows/papers/posts, never people (a
person's own podcast/newsletter is a source; the person stays in People);
names as given; no outside knowledge except one `(per model knowledge)`
clause when essential to identify the source.

## annotate-people (after the summaries)

Inputs: finished notes (packages or run dirs) and digests — never
transcripts. Scope: a collection (all its packaged episodes + digests), a
run dir, or a digest (its linked episodes); `--collection` adds the
collection's episodes and writes to its roots.

Outputs, written to the collection folder AND the vault subject folder:
`people/_index.md`, `people/<slug>.md`, `people/_resolutions.md`,
`sources/_index.md`. State (descriptions, resolutions, knowledge cutoff):
`<collection>/people/.annotate_state.json`.

| part | who |
|---|---|
| unique people: People rows of every episode/digest, `people.merge_people` (ASR sounds-alike, bare names, hedges) | deterministic |
| dropped: unnamed speakers, withheld names, winner/attendee/audience roles (never sent to the model) | deterministic |
| 2–3 sentence description of each NEW person, `known`/`private` flags | model, batched 20 per call |
| vague role references in the notes (titles like Secretary of State / Fed chair / FBI Director, `<Org>'s CEO`), excluding ones followed by a name | deterministic regex |
| who held the role on the episode date + confidence + description | model, same calls (20 roles per call) |
| "verify — after model knowledge cutoff" | model flag OR episode month > the model's own stated cutoff |
| person notes, index, appearances, "as stated in sources", sources index + listen/read-next queue, links + link check | deterministic |
| first mention of a resolved role in each episode's notes gets ` (→ [Name](../people/x.md), as of D, per model knowledge[ — verify …])` | deterministic; digests strip it before packing |

Calls: `ceil(max(new people, new roles) / 20)`. Existing people are never
re-described unless `--refresh`; a person the model flags private gets no
note; an unknown person gets "No reliable model knowledge; known only from
sources" (never a guessed bio). `--max-calls N` stops between batches; the
rest show "Description pending" and the next run picks them up. A rerun with
nothing new: 0 calls, every file byte-identical.

Listen/read next: sources ranked by times cited + episodes + a 30-day
recency bonus; podcasts/shows/videos/talks are "listen", the rest "read".
Candidate inputs only — what each was cited for is as the episodes state.

## Real run (2026-10-21)

Subscription chain only (`claude-cli/opus` → `claude-cli/sonnet`,
`ALLOW_METERED_FALLBACK=false`, `--usd-cap 0`); every call served by opus.

- **FSX5JFJS9PQ re-summarized** from its Stage 2 transcript (copy in
  `previous_run_results/AB_aidb/wp_p2/ep1_FSX5JFJS9PQ`, no ASR): 1 passage call
  (2 windows packed). One `## Sources Referenced`, 15 sources (posts on X and
  Reddit, The Information, Axios, a DOJ filing, a DHS memo, the Artificial
  Analysis personality bench, a blog essay, a video), 0 people rows among them.
  Eval P1–P7 all PASS (255/255 citations). Packaged: 255/255 linked, 0 broken.
- **W43 digest rebuilt:** 1 call, 140 citations kept, 0 dropped,
  `link_check: pass`; new "Sources of the week" (15 rows, linked).
- **annotate-people ai_daily_brief** (7 episodes W42+W43, 2 digests):
  124 unique people, **7 calls** (ceil(124/20)), 79 known / 44 "no reliable
  model knowledge" / 1 flagged private (no note); 37 ASR spellings got a
  probable spelling (Dario Amade → Dario Amodei, Shamath Palahapatia →
  Chamath Palihapitiya …). Role references: "OpenAI's CEO" → Sam Altman
  (high; verify — after cutoff 2026-06). Rerun: 0 calls, all files
  byte-identical. Link check: 0 problems over 126 files per root; all 7
  packages 0 broken links.
- **Caught by the real run, fixed:** "the President's executive order"
  matched "the President" and got annotated before "'s" (possessives are now
  excluded; the one bad annotation was removed by hand); "Brian Moynihan is
  Bank of America's CEO" was a candidate although named (named-before/after
  filter; multi-word orgs with "of"); quoted titles lost one quote in the
  sources merge.
- **Known interaction:** annotating a package's notes.md changes its hash, so
  a later `package` of the same episode keeps the vault copy ("user-edited")
  unless `--force`; re-run `annotate-people` after a forced re-package.
