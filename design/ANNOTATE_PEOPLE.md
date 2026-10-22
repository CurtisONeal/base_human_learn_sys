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

## Quote-first voices + role on date (WP-P4)

User goal: find the people whose opinions are high-signal in the AI space,
as sources; suggest a high-likelihood identity even when it is only an X
account. And fix WP-P3's role answer, which identified the person in the
sentence (the 2023 ousting) instead of the holder on the episode date.

Three lookup kinds, each with its own prompt, cache key and normaliser; all
share `--max-web-calls` (default 5), roles first:

| kind | when | prompt | cache |
|---|---|---|---|
| role | role flagged verify (post-cutoff) and not yet checked for the date | `role_on_date.md` | `rec["role_check"]` |
| person | a model bio flagged post-cutoff (WP-P3) | `web_lookup.md` | `rec["web"]` |
| voice | no reliable model knowledge and no adopted WP-P3 identity; never private / pending / role-created | `voice_lookup.md` | `rec["voice"]` |

### Voices (quote-first)

| part | who |
|---|---|
| payload per target: names, claim (People-row relevance), notes sentences that mention the name, `exact_quotes` (quoted spans ≥3 words), `episodes` with topic (title) + window (7 days before), `handle_candidates` (one-token names; spoken handles "Faw for AI" → fawforai; "B on X" → too short) | deterministic (`voice_payload`) |
| `queries`, in order: `site:x.com "<quote>" after:D-7 before:D+1` (≤2) → `site:x.com [multi-word name] <claim keywords> after… before…` → `x.com/<handle>` → `"<name>" <topic> AI` | deterministic (`voice_queries`) |
| search, read ≤3 results (3 more only if all fail), report evidence items `{url, kind, date, login_wall, supports: bio/topic/quote}` | model |
| tier from the evidence, never from the model's word: **confirmed** = handle/profile + `bio` evidence + the quote seen, `quote_url` among the evidence AND on their own x.com post/profile; **probable** = handle + `bio` + `topic` evidence (a quote seen elsewhere — someone's reply, an article — is probable); **unknown** otherwise. The model's tier is a ceiling (never upgraded); every downgrade gets a note | deterministic (`normalize_voice`; `enforce_voice_rules` is also re-applied to cached results on load) |
| confirmed → description adopted ("Description — per web lookup D (confirmed identity)"); probable → an "## Identity" section (handle link, display/real name, affiliation + type, evidence links with kind/login wall/date), the description stays "No reliable model knowledge"; unknown → the note, plus "Possible candidate, not adopted: @x" if the model named one | deterministic |

### AI voices table (`sources/_index.md`, both roots)

Rows: people with ≥1 episode row whose role is quoted / cited / guest /
speaker / author (never host), with an identity — voice tier confirmed or
probable, or a model / web description — whose **bio** (the description,
not what the notes say they talked about) matches an AI pattern. Columns:
person (note link) · handle (profile link) · real name · affiliation (type)
· tier · times quoted · distinct episodes · first/last quoted · recency
(days) · quoted posts (quote / post evidence) · where quoted (YouTube
timestamps) · score. Not ranked: quoted-but-unidentified (named in one
line) and a count of identified-but-not-AI people.

    signal score = Q × B × R × W
      Q = times quoted   (citations on the person's quoted rows, ≥1 per row)
      B = 1 + ln(distinct episodes)
      R = 0.5 ^ (days since last quoted / 30)
      W = lab 1.5 · researcher 1.4 · founder 1.3 · investor 1.2 · journalist 1.1 · other 1.0
          (voice: the lookup's affiliation_type; otherwise the first rule that
          matches the description: a named AI lab → lab; research/professor/
          scientist → researcher; founder/CEO/engineer/developer → founder;
          investor/VC → investor; journalist/writer/newsletter/podcast/analyst
          → journalist)

### Role on date

| part | who |
|---|---|
| question: "Who held <role> at <org> on <date>?" (`role_parts`: "OpenAI's CEO" → CEO / OpenAI; "the UK Prime Minister" → Prime Minister / UK); the notes sentence goes in `context_sentence`, labelled "context only … not the question" | deterministic |
| `last_confirmed` = model answer as of the knowledge cutoff; `evidence_window` = [date − 90 d, date]; `change_window` = [cutoff month, date] | deterministic |
| holder + sources, each with its own date/kind/live; a change-check search ("<org> new <role>", "steps down", "appointed") → `changes` | model |
| **confirmed** only if ≥1 source is dated inside the evidence window, or a change is dated inside it, or a live official leadership page while the episode is ≤90 days old (a live page shows today's holder); else **last confirmed <newest dated source ≤ episode date>** and verify stays | deterministic (`normalize_role_date`) |
| a change dated after the last confirmation and ≤ the episode date wins (name = new holder; previous holder recorded; "answer named X; the change found wins" when the model's own answer disagreed); a change outside the window is ignored | deterministic |
| a differing holder WITHOUT a recorded change = contradiction: model answer kept in state, shown with "?" and "model knowledge: X — verify" | deterministic (`apply_web_role`) |
| a WP-P3 `rec["web"]` role answer (undated sources) never clears verify any more ("undated sources — verify"); the role is re-checked once | deterministic |
| provenance in notes / index / log: "per web lookup D; [changed D2: X (event; previously Y);] confirmed by a source dated D3 \| last confirmed D3; sources: [host D](url), …" — inside the `(→ …)` annotation that digests strip | deterministic |

Tests: `tests/test_annotate_voices.py` (quote-first queries, two-signal
tiers, login wall → probable, own-post rule + cache re-evaluation,
candidate, scoring/ranking, role question, date acceptance/rejection,
change override, WP-P3 role migration, cache/idempotence, ≤5 calls,
privacy); `tests/test_annotate_web.py` updated to the kind-aware mock.

## Real run — WP-P4 (2026-10-21)

Subscription chain only (`claude-cli/opus` → `claude-cli/sonnet`,
`ALLOW_METERED_FALLBACK=false`, `--usd-cap 0`), ai_daily_brief W42+W43
(7 episodes, 2 digests). **4 web calls** in total (1 role + 3 voice, all
served by `claude-cli/opus`), 0 model-knowledge calls. Run 1:
`--max-web-calls 1` (the role); run 2: `--max-web-calls 3` (28 voice
targets = the 27 WP-P3 "not found" + 1 WP-P3 low-confidence match, Alex
Finn).

- **Role "OpenAI's CEO" (2026-10-16): Sam Altman — confirmed by a source
  dated 2026-09-14** (cnbc.com 2026-09-14, bloomberg.com 2026-09-03, an
  undated unifygtm.com page); change check: no change found 2026-06 →
  2026-10-16; the CLI's confidence: medium. Caveat (observed, from the
  reply's own note): none of the 3 pages could be read (2 fetches got 403,
  1 not tried) — the dates and statements come from search-result summaries
  and URL paths. The WP-P3 answer (two 2023 articles about the ousting)
  stays in state as history and no longer counts.
- **Voices, the 27:** confirmed 2, probable 3, unknown 22 (Alex Finn, the
  28th: unknown). Confirmed: Kyo → [@theo](https://x.com/theo) (the notes'
  "Kyo" is the ASR form of Theo; the quote is on his own post), Websim
  Surreya → [@kickingkeys](https://x.com/kickingkeys) (Surya, Websim).
  Probable: George Cal → @georgevibing (quote seen only in a snippet of
  @elonmusk's reply), Simon Smith → @_simonsmith (quote seen in a digg.com
  write-up), Chetislua → @chetaslua (handle + AI posts; the topic evidence
  is other accounts' posts in the thread). Every x.com evidence item was a
  login-walled search snippet.
- **Caught by the real run, fixed:** the CLI tagged George Cal and Simon
  Smith "confirmed" with the quote seen on someone else's post / an article
  → the own-post rule (`enforce_voice_rules`), re-applied to the cache with
  0 calls; "Emil Michael is the U.S." (sentence split after "U.S.") →
  `first_sentence`; the "lab" weight matched any mention of Google / Meta /
  Microsoft, and a stated background made a judge "lab" → named AI labs
  only, and the bio alone decides AI relevance and type.
- **Known limits (observed):** Kyo (@theo) and Theo (model knowledge) are
  the same person in two rows — a lookup does not merge index entries yet;
  a description that mentions an AI event (Judge Alex G. Tse, an OpenAI
  engineer's bail hearing) still counts as AI-related.
- Rerun: 0 model calls, 0 web calls, 299 output .md files byte-identical
  (sha256 over both roots); link check: 0 problems.
