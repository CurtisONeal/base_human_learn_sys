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

## Web lookup (WP-P3)

User rule (approved): "When there is no model knowledge, or the context is
past the model's knowledge cutoff, a web lookup call is approved — but keep
the search and the summary succinct: restrict to the 3 most authoritative
likely sources only, unless each of those fails."

| part | who |
|---|---|
| targets: roles whose state record has `verify` (episode month > cutoff or model flag), then people with `known: false` (or a `verify` flag), multi-word names before bare handles, most appearances first | deterministic (`select_web_targets`) |
| never: private (model flag), unnamed / withheld / winner / attendee rows (never in the index), confident pre-cutoff model knowledge, anything already looked up | deterministic |
| lookup: 10 targets per call, `--max-web-calls` (default 5) | model, `prompts/annotate/web_lookup.md` |
| source budget: 3 most authoritative likely sources (official org / government record → reference work or major outlet profile → own verified profile), stop when 2 agree, 3 more only if all 3 fail | prompt; ≤3 URLs (≤6 with `fallback_used`) enforced in `normalize_web` |
| "found" without a URL, or a role without a holder → "not_found" | deterministic |
| a LOW-confidence identity match is never adopted as the description: the note keeps "No reliable model knowledge" and shows "Possible match, per web lookup … — low confidence, not adopted" (added after the first real call: the CLI returned a low-confidence "identified only by the name fitting the context" match) | deterministic (`_web_found`) |
| cache: `rec["web"]` on the person / role record in `.annotate_state.json` (status, name, description, sources, confidence, fallback, agrees_with_model, looked_up_at, model); kept across `--refresh` | deterministic |
| provenance: person heading "Description — per web lookup <date> (sources: …)", index tag "(web lookup)"; role "→ X, as of D, per web lookup <date>; sources: …" in notes, index and log; not-found noted beside "No reliable model knowledge" | deterministic |
| verify cleared only when the lookup confirms (same holder as model knowledge, or model had none; high/medium confidence). Different holder = contradiction: state keeps the model answer, output shows the web answer with "?" + "model knowledge: X — verify" | deterministic (`apply_web_role`) |
| stale in-notes annotation (model knowledge → web lookup) is replaced in place; digests strip both forms | deterministic |

CLI call (`summarizer.complete_web_lookup`, claude CLI 2.1.306):
`claude -p --model <alias> --system-prompt-file F --tools WebSearch,WebFetch
--allowedTools WebSearch,WebFetch --disallowedTools Skill,Read,Bash,…
--strict-mcp-config --output-format json`, API keys stripped, scratch cwd.
Only `claude-cli/*` entries of `LLM_MODELS` are tried — web tools exist only
in the CLI, so a web lookup can never reach a metered backend whatever
`ALLOW_METERED_FALLBACK` says. Exhaustion stops the lookup (finished
batches stay cached).

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

## Real run — web lookup (WP-P3, 2026-10-21)

`annotate-people ai_daily_brief --usd-cap 0` with `LLM_MODELS=[claude-cli/opus,
claude-cli/sonnet]`, `ALLOW_METERED_FALLBACK=false`; first `--max-web-calls 1`
as a smoke test, then `--max-web-calls 4`. **5 web calls** in total, all
served by `claude-cli/opus`; 0 model-knowledge calls (nothing new).

- Targets: 45 (1 post-cutoff role + 44 "no reliable model knowledge" people;
  the 1 private person and the unnamed/winner rows were never sent).
- People: **16 found** (high/medium, adopted with sources), **1 low-confidence
  possible match** (shown, not adopted), **27 not found** (mostly bare
  handles and names the notes say nothing about). 0 used the fallback; every
  answer has ≤3 sources.
- ASR spellings corrected by the lookup: Pavel Huran → Pawel Huryn, Liv
  Gordon → Liv Gorton, Ellie Bukouch → Elie Bakouch, Alex Emas → Alex Imas,
  Abi Desai → Abhi Desai, Judge Alex C → Alex G. Tse, Abdul Rahman Alphazan →
  Abdulrahman Alfozan.
- Role "OpenAI's CEO" (2026-10-16): **Sam Altman, high, agrees with model
  knowledge → verify cleared**. Caveat (observed): the sentence is about the
  late-2023 ousting, and the two sources the CLI returned are Nov 2023 news
  pieces (aljazeera.com, wfsb.com) — they identify the person referred to,
  not who held the role on 2026-10-16. The prompt asks for the holder on the
  episode date; it answered the sentence instead.
- Observed source quality: most answers came from search-result summaries
  ("pages not opened directly"); some sources are aggregators (dealroom,
  yespress.io, happenstance.ai). The confidence + sources are in each note,
  so this is visible, not hidden.
- Rerun: 0 model calls, 0 web calls, no file written. Link check: 0 problems.
