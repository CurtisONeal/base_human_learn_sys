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
