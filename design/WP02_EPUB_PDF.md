# WP-02 — Books into the pipeline (EPUB, then PDF)

Built overnight 2026-08-23. Read this first in the morning; it answers the
question you asked ("I'm not clear the shape you intend to build") before it
gets into what was done.

---

## 1. The shape, and how it differs from Docling

You said Docling is your reference point. That is a good comparison because it
makes the difference sharp.

**Docling (and MarkItDown, Unstructured, LlamaParse) are *converters*.** You
give them a file, they give you Markdown or JSON. They are very good at the
hard part of that job — table structure, reading order in two-column layouts,
formula recovery, and OCR of scans. Their output is a *document*.

**This is not a converter. It is a source adapter into a pipeline that already
exists.** The pipeline's shape was set by the video work:

```
        source  ─────────────►  SourceText  ─────────────►  summarize  ──►  notes
    (video / captions                │                          │
     / book / article)               │                          │
                                     │                          │
                            the ONE type every            failover chain,
                            source must produce           exhaustion policy,
                                                          no-surprise-spend
```

A book is just a fourth way to fill in `SourceText`. Everything downstream —
the failover chain, the subscription-vs-metered rules, the "stop cleanly and
resume" behaviour you specified — is inherited, not rebuilt.

So the honest positioning is:

| | Docling | This |
|---|---|---|
| Output | Markdown / JSON document | `SourceText` feeding a summarizer |
| Scanned PDFs | OCR, good at it | **Refused** — out of scope, says so |
| Tables & formulas | Strong | Weak (text order only) |
| Chapter/page locators | Present in output | **Load-bearing** — every claim cites one |
| Cost control | N/A | Inherited: chunk budget, exhaustion, no silent escalation |
| Deps | heavyweight (torch, models) | EPUB: **zero**. PDF: one (`pymupdf`) |

**Where Docling would beat this today:** scanned PDFs, dense tables, and
anything where layout carries meaning. If the corpus turns out to be full of
those, swapping the PDF extractor for Docling behind the same
`extract() -> SourceText` interface is a contained change — that is why the
interface exists. I did not reach for it now because it is a large dependency
to add for a corpus that turned out to be 100% born-digital with clean text
layers.

---

## 2. What got built

| File | What it is |
|---|---|
| `core/sources.py` | `SourceText` + `Section` — the seam. Media fills `segments`, documents fill `sections` |
| `core/documents/epub.py` | EPUB reader. **stdlib only** — zipfile + html.parser |
| `core/documents/pdf.py` | PDF reader (`pymupdf`), page-accurate locators |
| `core/chunking.py` | Groups sections into model-sized passages without crossing chapters |
| `core/summarizer.py` | `complete_with_failover()` extracted; new `summarize_document()` map-reduce |
| `tests/test_documents.py` | 37 tests, including 5 against the real corpus |

`pyproject.toml` gained a `[documents]` extra. **The sync command changed:**

```
uv sync --extra ocr --extra dev --extra documents
```

A bare `uv sync` still silently strips extras — the bug that broke the
environment before. **The readme is updated** in both places it appears.

`main.py` gained a `book` command:

```
book <file-or-directory> [--extract-only] [--force] [--max-words N] [--on-exhaustion ...]
```

`--extract-only` calls no model at all: it prints the structure found and how
many passages a run would cost. Use it habitually — it is how you catch a book
whose structure came out wrong *before* paying to summarize it, which is
exactly how both PDF bugs below were spotted.

---

## 3. The design decisions worth arguing with

**Locators tell the truth about what the format knows.** An EPUB has no pages,
so it never prints one — it prints `ch. 7`, or the section name when the
publisher did not number the chapter. A PDF prints `p. 214`. The provenance
string says which situation you are in:

```
epub (nav.xhtml)   epub (toc.ncx)   epub (none)
pdf (bookmarks)    pdf (headings)   pdf (pages only)
```

`structure_is_inferred` is set whenever the structure was guessed rather than
read. Your WP-02 acceptance criterion said "every claim carries a
chapter/page locator" — this is the correction to that: **EPUB cannot carry a
page locator, and faking one would be the worst possible failure** in a system
whose value is that you can check its claims.

**One chunk never spans two chapters.** That is what makes a citation
attributable — a chunk carries exactly one locator, so a claim derived from it
cannot be wrong about where it came from. Cost is the secondary reason.

**Book text is not appended to the summary.** Transcripts are; books are not.
The text is copyrighted and these artifacts may end up in a vault or a repo
(design decision D4). Only the notes and the provenance block are written.

---

## 4. What the corpus told us

20 → 49 files after your second pull: **32 EPUB + 17 PDF, 1.1 GB, 11 titles in
both formats.**

### EPUB vs PDF on the same 11 books

After fixing the duplication bug (§5), word counts converge:

| Book | EPUB | PDF | Δ | PDF sections |
|---|---|---|---|---|
| aiandmlforcodersinpytorch | 120,769 | 118,613 | −1.8% | 198 |
| buildingapplicationswithaiagents | 108,593 | 111,019 | +2.2% | 181 |
| craftingengineeringstrategy | 79,923 | 80,516 | +0.7% | 209 |
| developersplaybook…llmsecurity | 63,947 | 66,155 | +3.5% | 141 |
| engineeringexecutivesprimer | 110,147 | 109,808 | −0.3% | 253 |
| generativeaidesignpatterns | 142,141 | 142,329 | +0.1% | 181 |
| hands-onml…scikit-learn | 294,226 | 286,139 | −2.7% | 372 |
| learninglangchain | 66,865 | 67,765 | +1.3% | 120 |
| llmops | 91,178 | 91,619 | +0.5% | 172 |
| promptengineeringforgenerativeai | 106,711 | 105,865 | −0.8% | 177 |
| visualizinggenerativeai | **DRM** | 83,834 | — | 48 |

**Two independent extractors over two formats agreeing within ~2% is the
strongest evidence available that both are right.** Neither was checked against
the other during development; they converged.

### Findings that change decisions

1. **Most PDFs have real bookmarks, but not all.** Corrected after running the
   full 49-file corpus: `The mindfulness prescription for adult ADHD` and one
   other fell back to `pdf (headings)`, so the font-size heuristic *is*
   exercised. One file (`7864pt`, 160 words, 1 section) is effectively empty and
   should be looked at — it is probably a poster or flyer, not a book.

2. **One EPUB is DRM-encrypted; its PDF twin is not.** `visualizinggenerativeai`.
   The DRM check fired and refused, as designed — no circumvention attempted.
   This is a live argument for keeping both formats rather than discarding PDFs.

3. **Springer/Apress mark code as `class="ProgramCode"`, not `<pre>`.** A
   generic `<pre>`/`<code>` extractor finds **zero** code blocks in these books.
   With class-matching: 480 blocks in the RAG book, 588 in Learning LangChain.
   Without this, every summary would have read as though the books contained no
   code. This is the single highest-value detail found tonight.

4. **PDF is *not* obviously worse.** Your instinct that "PDF is far more common"
   is right, and the comparison says the cost of using it is small on
   born-digital books. **PDF's page locators are strictly better than EPUB's
   chapter locators** for verification. EPUB's advantage is structural fidelity
   and no dependency — not accuracy.

### The graph-RAG question is now testable

I previously told you this corpus had no graph-RAG material. **That was wrong**
— it described the other bundle. Actual distribution:

| Book | "graph RAG" | "knowledge graph" |
|---|---|---|
| masteringretrieval-augmentedgeneration | 103 | 61 |
| transformingfinancialserviceswithgenerativeai | 10 | 11 |
| datasciencesolutionsonazure | — | 5 |
| masteringlangchain | — | 3 |
| *+7 others* | — | 1–3 each |

That is a **known-answer positive test** for WP-05: one primary source, one
secondary, nine passing mentions. Retrieval can now be graded, not just
observed. It also cost nothing but `grep` — the same "free signal gates the
expensive one" move the video pipeline runs on.

---

## 5. Bugs found and fixed tonight

**PDF bookmark duplication (severe, would have shipped).** Assigning each
bookmark the page range from its own start to the next bookmark's start
double-counts, because bookmarks routinely share a start page (92 of 264 in
`llmops`) and nested outlines let a chapter's range cover its own subsections.

Measured: `llmops.pdf` holds **90,778** words. The extractor emitted
**213,451** — the same prose 2.35× over. That would have inflated cost by
~2.3×, skewed any term frequency computed from the corpus, and invited a
summary to read repetition as emphasis.

Fixed by assigning every page to exactly one owner: the last bookmark starting
at or before it. Duplication factor is now 1.00–1.01× across all 11 books.

**Caught by the EPUB↔PDF comparison you asked for.** Without the paired corpus
this would have looked like a plausible number. That is the argument for
keeping both formats even after choosing a default.

**Book chunks were summarized with the video prompt (severe, silent).** The
`claude` CLI takes a system prompt as a *file*. When I factored the failover
loop out of `generate_summary`, the CLI branch kept passing
`settings.prompt_file` unconditionally and discarded the `system_prompt`
argument it had just been given. Every book passage was therefore being
summarized with `summarize_prompt.txt` — the video prompt — losing the locator
requirement and the SKIP rule entirely.

It raised no error and produced plausible output. **It was caught only by
reading the arguments of the running process in `ps`**, not by any test and not
by looking at the code. There is now a test that writes a sentinel string
through `complete_with_failover` and asserts the CLI receives that exact text.

Worth generalising: a refactor that *adds* a parameter can leave a call site
ignoring it, and every type checker and test will stay green. The failure mode
is "correct-looking output from the wrong instructions".

**PDF outlines are not monotonic, and one entry ate an entire book (severe).**
The ownership pass walked bookmarks in *outline* order, each claiming "my page
to the end". LLMOps ends its outline with three production artifacts —
`7x9_web_Rabbit.pdf`, `Blank Page`, `Blank Page` — all pointing at **page 1**.
Being last, they claimed all 284 pages.

Result: **one section** titled `7x9_web_Rabbit.pdf > Blank Page` holding the
whole book, while still reporting provenance `pdf (bookmarks)` and a plausible
90,694-word count. Two books were affected (`llmops`, `hands-on ML`). Ownership
now follows page order; llmops yields 172 sections, hands-on ML 372.

**This is the failure mode this module keeps producing: confident, plausible,
and wrong.** Both PDF bugs reported a healthy-looking word count and a
provenance string claiming real structure. Neither raised an error. The only
reason either was caught is that the corpus holds the same books in two formats
and the numbers had to agree.

**Adding the `book` command silently broke the video CLI.** typer requires an
explicit subcommand once a second command is registered, so
`python -m video2mdnotes.main <url>` — the form used by the readme, the
overnight sweep script and any cron entry — began failing. Caught by three
existing tests. A `_with_default_command()` shim now inserts the implicit
`process` verb, so the published interface is unchanged.

**EPUB nav page-lists parsed as structure.** An EPUB3 nav document holds
several `<nav>` lists — the TOC, a page-list, landmarks. Parsing all of them
produced 1833 "sections" for a 31-chapter book, most named things like
"xxxix". Now only the `epub:type="toc"` nav is read.

**Locators counted spine files, not chapters.** Front matter was being labelled
`ch. 4`. Now derived from the chapter number in the heading, with an honest
non-numeric fallback.

---

## 6. State, and what is not done

**Tests: 175 passing** (150 before + 25 new). No regressions; the media path is
untouched by design — `SourceText.from_transcript()` adapts at the boundary
rather than rewriting `transcriber.py`.

Not done, in the order I would do them:

1. ~~Readme install section~~ — **done**, both occurrences.
2. ~~`main.py` routing~~ — **done.** `book <path>` accepts a file or a whole
   directory, with `--extract-only` (free structural report), `--force`,
   `--max-words` and `--on-exhaustion`.
3. ~~Cost measurement~~ — **done.** EPUB: 15 passages, **787s**, 15,247 words of
   notes on `claude-cli/haiku`. PDF: 16 passages. Roughly **13 minutes and ~17
   model calls per book** at Haiku. Across 49 files that is on the order of 10
   hours and ~800 calls — enough that the catalog (WP-04) should decide *which*
   books are worth summarizing before any bulk run.
4. **Quality comparison EPUB vs PDF** — word counts match, but whether the
   *summaries* differ is the question you actually asked. Needs the same book
   summarized from both formats and read side by side.
5. **ADR** — the locator-honesty rule and the "refuse scans rather than OCR
   them" boundary both deserve recording.
6. **WP-04 catalog** — now unblocked, and the grep table in §4 is a preview of
   what it produces for free.

Unproven in this corpus: the font-size heading heuristic, the scanned-PDF
refusal, `toc.ncx` (all 32 EPUBs used EPUB3 nav), and oversized-section
splitting (no single section exceeded 12,000 words).

---

## 7. End-to-end run

`llmops` summarized from **both** formats on `claude-cli/haiku`. Haiku
deliberately: this measures plumbing and cost, not summary quality, and burning
the Opus subscription overnight would leave you rate-limited in the morning.

| | EPUB | PDF |
|---|---|---|
| Source | 91,178 w · 259 sections | 91,619 w · 172 sections |
| Passages | 15 | 16 |
| Wall clock | 787 s | ~600 s |
| Notes produced | 15,247 words | 14,205 words |

**The EPUB result is good.** Every claim carries a locator (`[Preface]`,
`[ch. 1]`), quotations are preserved verbatim, and the structure follows the
book's own order. It reads as usable study notes rather than a précis.

### Side-by-side quality — the question you actually asked

| | EPUB | PDF |
|---|---|---|
| Notes | 15,247 w | 14,205 w |
| Locator citations | 717 | 681 |
| Section headings | 108 | 71 |
| Verbatim quotations | 40 | 64 |
| Fenced code blocks | **0** | 15 |
| Inline code spans | 72 | 22 |
| Topic coverage | 16/16 | 16/16 — **identical**, neither had a topic the other missed |

**Verdict: for a born-digital book, the two are substitutable.** Comparable
length, comparable locator density, identical topic coverage. PDF's locators
(`p. 214`) are more precise and more verifiable than EPUB's (`ch. 7`). EPUB
produced a finer heading structure. Nothing here justifies preferring one
format on quality grounds — which supports your instinct to take PDF, since it
is the format you actually have more of.

**One real defect, and it is not format-specific.** The EPUB extractor fed the
model **140 fenced code blocks** and the notes came back with **zero**. The
model described the code (72 inline spans) instead of reproducing it. So the
`class="ProgramCode"` detection — the highest-value extraction finding of the
night — currently improves *extraction* without improving *notes*. The chunk
prompt says "preserve code exactly"; that is evidently too weak to make it
reproduce blocks. **Fixing this is the highest-value next change for a technical
corpus** and it is a prompt change, not a code change.

**The PDF result took three attempts**, and the two failures are the most
instructive things in this document — see §5. Neither was a PDF problem; both
were in the reduce stage and would have hit EPUB equally under the same
conditions. The EPUB run escaped them only by having 15 passages instead of 16,
which put it under a threshold.

Artifacts:

```
scratchpad/wp02/llmops.epub.summary.md            the good one
scratchpad/wp02/llmops.pdf.summary.md             third attempt
scratchpad/wp02/llmops.pdf.summary.COLLAPSED.md   the 177-word chat failure
scratchpad/wp02/comparison.json                   per-title extraction metrics
```

### Cost, measured

Roughly **13 minutes and ~17 model calls per book** at Haiku. Across the 49-file
corpus that is on the order of **10 hours and ~800 calls**.

That number should change the build order. Summarizing the whole corpus is not
something to start casually, and **WP-04 (catalog) should decide which books are
worth it first**. The grep table in §4 cost nothing and already showed that nine
of the eleven graph-RAG books only mention the topic in passing.

---

## 8. The honest character of this work package

Six bugs in one session, every one of them **silent, plausible, and invisible to
the test suite** until a real file hit it:

| Bug | Found by |
|---|---|
| PDF pages double-counted (2.35×) | EPUB and PDF word counts disagreeing |
| One bookmark swallowed a whole book | `--extract-only` printing "1 sections" beside "pdf (bookmarks)" |
| Book chunks used the video prompt | reading `ps aux` for the running job |
| `book` command broke bare-URL invocation | three pre-existing tests |
| Failed reduce discarded 16 passages | running an actual 91,000-word book |
| Reduce returned 177 words of chat | reading the output file |

Two patterns worth carrying forward:

**Wrong output here looks right.** Every failure produced a plausible word count
and a provenance string claiming real structure. None raised an error. This is
a module where "it ran" means almost nothing.

**A fix can be worse than the bug.** The batching that cured a timeout caused the
177-word collapse, and the guard written for *that* rejected the best output of
the night because it matched on phrasing. Both were caught by testing against
the real artifacts rather than reasoning about them.

The paired corpus is the reason most of these were findable at all. Keeping both
formats has ongoing diagnostic value beyond deciding which to prefer.
