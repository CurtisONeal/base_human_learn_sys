# Fast Video Notetaker v2

The point of this project is to have a robust Python application that, when provided the URL of a video or video playlist, will download the audio, create a transcript, and use an LLM to generate a structured summary.

This project has been refactored from a collection of scripts into a modern, containerized Python application.

## Platform support — read before installing

| Feature | macOS | Linux / Windows |
|---|---|---|
| Download, transcribe, summarize | ✅ | ✅ |
| Captions-first sourcing | ✅ | ✅ |
| Keyframe extraction (ffmpeg) | ✅ | ✅ |
| **Local OCR of those frames** | ✅ free | ❌ **not implemented** |

**The core pipeline is cross-platform. Visual content extraction is not.**

Local OCR uses the **macOS Vision framework** — free, offline, fast, and good on
code and UI. There is no equivalent wired up for other platforms, and this is
not a cosmetic gap: OCR is what makes visual extraction nearly free, because it
handles most frames so the paid vision model only sees the ones it cannot. On
Linux or Windows **every keyframe escalates to a metered vision-model call**,
inverting the feature's economics. The pipeline warns loudly when it detects
this, but it cannot fix it.

Non-macOS users have three options, in order of preference:

1. **Leave `EXTRACT_FRAMES=false`** (the default). Everything else works
   normally; you simply get no visual content.
2. **Set `VLM_ENABLED=false`.** Frames are extracted and embedded in the notes
   as images, with no OCR text and no spend — useful if you just want the
   screenshots.
3. **Accept the cost.** Set `EXTRACT_FRAMES=true` and budget with
   `VLM_MAX_FRAMES`. Expect roughly 3-15× the macOS cost depending on content.

### Adding an OCR backend — no fork required

`OCR_BACKEND` already exists as the switch (`auto` | `none`); it simply has no
non-macOS implementation behind it yet. Adding one means a new branch in
`ocr_available()` / `ocr_image()` in `core/visuals.py` returning plain text —
Tesseract (weak on code and UI) or PaddleOCR (better, heavier install) are the
obvious candidates.

**Please add a backend rather than forking.** This codebase already carries the
cost of one hand-maintained fork (`base_human_learn_sys`), where every fix has
had to be applied twice and one was missed for six weeks. A second divergence
for a platform difference that a single function can absorb would repeat that
mistake.


## Project Structure
The project now follows a standard Python package structure:
```
video2mdnotes/
├── .clinerules/         # Internal documentation and project plans
├── .dockerignore        # Files to ignore for Docker builds
├── .env.example         # Template for environment variables
├── .gitignore           # Files ignored by Git
├── docker-compose.yml   # Defines the application service for Docker
├── Dockerfile           # Instructions to build the application container
├── human_readable_documentation/ # User guides and project documentation
├── legacy_scripts/      # The original, now-obsolete scripts
├── previous_run_results/ # Default output directory for processed videos
├── pyproject.toml       # Project definition, dependencies, and tools
├── src/                 # Main application source code
│   └── video2mdnotes/
├── summarize_prompt.txt # The prompt used for the LLM summarizer
└── tests/               # Automated tests for the application
```

### Package Management

> ⚠️ **Use `uv sync --extra ocr --extra dev --extra documents --extra semantic --extra asr-mlx`, not a bare `uv sync`.**
> OCR, dev and document dependencies are optional extras, so a bare sync silently
> removes `ruff`, `pytest`, `ocrmac`, `imagehash`, `Pillow`, `pymupdf` and `fastembed` — leaving
> a checkout that looks fine but has no lint, no tests, no visual extraction,
> no PDF support, and no semantic search.

This project uses modern Python packaging tools:
- **uv**: A fast, reliable package installer and virtual environment manager.
- **hatchling**: The build backend used for packaging the application.
- **pyproject.toml**: The single configuration file for defining the project and its dependencies.

## How a run decides what to do

Every branch below is a real decision point with a config flag behind it. The
ordering is the whole design: **free signals gate expensive ones.**

```
URL
 │
 ├─ probe_source()                      metadata only, no download
 │   ├─ yt-dlp handles it? ──── no ──▶ SCRAPE_PAGE_EMBEDS=true?
 │   │                                  ├─ yes ─▶ fetch page, probe each embed
 │   │                                  └─ no  ─▶ fail with yt-dlp's error
 │   └─ playlist?  ──▶ GROUP_BY_PLAYLIST=true ─▶ notes go in <playlist>/ 
 │
 ├─ per video ─ already summarized?  ──▶ yes & not --force ─▶ SKIP (resume)
 │
 ├─ TRANSCRIPT ─────────────────────────────────────────────────────────────
 │   CAPTIONS_FIRST=true?
 │    ├─ manual track in CAPTIONS_LANG?      (auto-captions never used)
 │    │    ├─ ≥ CAPTIONS_MIN_WORDS?  ──▶ USE CAPTIONS   (no download, no Whisper)
 │    │    └─ below threshold        ──▶ fall through   (music/[♪♪♪] tracks)
 │    └─ none ──▶ fetch_audio() ──▶ Whisper (local, free)
 │                                    └─ 0 segments ─▶ placeholder, no LLM call
 │
 ├─ SUMMARY ────────────────────────────────────────────────────────────────
 │   walk LLM_MODELS in order:
 │    ├─ claude-cli/*     subscription, no key   (tools OFF, scratch cwd)
 │    │     └─ exhausted? ─▶ ON_EXHAUSTION: wait | metered | local | stop | ask
 │    │                       default `wait` — never spends unasked
 │    ├─ openai|anthropic/*   metered, needs a real key (placeholders rejected)
 │    └─ ollama|lm_studio/*   local, no key — requires ALLOW_LOCAL_MODELS
 │
 ├─ VISUALS (EXTRACT_FRAMES=true only) ─────────────────────────────────────
 │   download low-res video
 │    ├─ scene detect @ FRAME_SCENE_THRESHOLD
 │    │    └─ < FRAME_MIN_BEFORE_INTERVAL frames? ─▶ interval sampling fallback
 │    ├─ perceptual-hash de-duplicate
 │    ├─ OCR every frame                       FREE, macOS Vision only
 │    ├─ eligible = OCR words < VLM_ESCALATE_BELOW_WORDS
 │    └─ rank eligible (dwell + uniqueness + audio cue)
 │         └─ top VLM_MAX_FRAMES ─▶ vision model   ← the only per-frame cost
 │
 └─ ARCHIVE ────────────────────────────────────────────────────────────────
     summary → visuals → transcript      (one insert_visual_section, both paths)
      ├─ no usable notes? ─▶ rename dir to no_summary_*   (MARK_EMPTY_RESULTS)
      └─ audio kept unless --keep-wav=false     (wav dwarfs the notes: 339MB vs 268KB)
```

**The load-bearing rule:** anything that costs money sits behind something that
does not. Captions gate Whisper; OCR gates the vision model; ranking decides
which frames are worth the vision model at all; and subscription capacity is
never silently traded for metered billing.

## Dependency maintenance (read this before debugging a failing run)

This pipeline depends on a moving target. YouTube changes; `yt-dlp` chases it;
a stale pin means **every download fails with HTTP 403** — and it presents as a
site problem or an IP block, not as a local one. That misdirection is what makes
it expensive: the instinct is to investigate the content, which never helps.

**Three dependency incidents in six days** (2026-08-16 → 2026-08-22): yt-dlp
eight months stale, yt-dlp six weeks stale, and a yanked numpy under the
transcription engine. Treat this as routine maintenance, not bad luck.

### First diagnostic: is it us or them?

Before investigating anything about the video, **retry a source that worked
before**. This is the whole trick.

```bash
# A video known to have downloaded successfully in the past
.venv/bin/python -m video2mdnotes.main "<previously-working-url>"
```

- **It fails too** → the cause is upstream. Stop looking at the content. Update.
- **It still works** → the problem really is that video (private, geo-blocked,
  region-locked). Investigate normally.

### The update runbook

Run these in order. Steps 2 and 4 are the ones people skip, and skipping either
means the fix does not survive.

```bash
# 1. Update the dependency IN THE LOCKFILE, not just the venv.
uv lock --upgrade-package yt-dlp          # or: --upgrade-package numpy

# 2. Sync WITH THE EXTRAS. A bare `uv sync` silently removes ruff, pytest,
#    ocrmac, imagehash, Pillow and pymupdf — leaving a checkout that looks fine
#    but has no lint, no tests, no visual extraction, and no PDF support.
#    (EPUB needs no extra: it is stdlib zipfile + html.parser.)
uv sync --extra ocr --extra dev --extra documents --extra semantic --extra asr-mlx

# 3. Fast check.
.venv/bin/python -m pytest tests/ -q -m "not integration"

# 4. Slow check — the one that actually exercises downloads and Whisper.
#    ~5 minutes. Unit tests cannot catch a dependency break; they mock it.
.venv/bin/python -m pytest tests/ -q -m integration

# 5. Commit the lockfile. An un-committed lock change is undone by the next sync.
git add uv.lock && git commit -m "Update <pkg>: <symptom it fixed>"
```

### Why each step exists (all three were learned the hard way)

| Trap | What happens |
|---|---|
| Updating the venv only (`uv pip install -U`) | The next `uv sync` silently reinstalls the broken version. The lockfile pin is what made it stale in the first place. |
| Bare `uv sync` | Removes the optional `ocr` and `dev` extras. Lint, tests, and visual extraction vanish without an error. |
| Trusting unit tests | They mock the network. All 136 passed while every real download was 403ing. |
| Reading one line of `uv.lock` | Versions are listed **per Python version**. `numpy` had both `2.2.6` (py<3.11) and the yanked `2.4.0` (py≥3.11); reading the first match reports the wrong answer. |

### Cadence

`yt-dlp` broke twice in four days at six weeks and eight months stale. There is
no safe interval — check it whenever downloads fail, and bump it opportunistically
whenever you are already touching the lockfile.

## Project Setup

### Prerequisites
- **Docker**: Docker Desktop (for Mac/Windows) or Docker Engine (for Linux) must be installed and running.
- **A summarization backend**: either a logged-in `claude` CLI (subscription, no
  API key), an LLM provider API key, or a local model server. See
  [Choosing a summarization backend](#choosing-a-summarization-backend) — this is
  a real setup decision, not a default to skip past.

### Choosing a summarization backend

The model chosen here does **not** affect transcription accuracy at all — but it
substantially changes how good the notes are, and it is easy to end up with a
weaker model doing the work without noticing.

#### The pipeline has two separate model steps

| Step | What runs it | Your choice? |
|---|---|---|
| **Transcription** (audio → text) | `faster-whisper`, locally on CPU | Model auto-selected between `large-v3` (multilingual, default) and `distil-large-v3` (English, far-field audio) from free signals — see `ASR_AUTO_SELECT` in `.env.example`. No API, no key, no network. |
| **Summarization** (text → notes) | An LLM, via one of the backends below | **Yes — this section.** |

Nothing in this section touches transcription. A "cheaper model" here means
thinner *notes*, never a worse *transcript*.

#### The chain

`LLM_MODELS` is an ordered **failover** chain, not a quality ladder. Entry 1 does
essentially all the work; later entries fire only when an earlier one *errors*.
**Putting a cheap model first makes it your summarizer.** The default:

```
LLM_MODELS=["claude-cli/opus", "claude-cli/sonnet", "openai/gpt-4o"]
```

**1. Subscription CLI (`claude-cli/…`) — no metered cost**

Shells out to the `claude` CLI, which authenticates with your logged-in Claude
subscription (macOS keychain) rather than an API key. Requires the CLI installed
and logged in. Bounded by your subscription's rate limits, which is exactly why a
metered entry sits behind it in the default chain. Aliases: `opus`, `sonnet`,
`haiku`.

**2. Metered API (`openai/…`, `anthropic/…`) — billed per token**

Standard REST via litellm using `OPENAI_API_KEY` / `ANTHROPIC_API_KEY`. No rate
ceiling, but every run costs money and a full transcript is a large prompt. These
keys are often shared with other tools on the same machine, so the usage lands on
the same bill.

**2b. OpenAI-compatible: Nous (`nous/…`) and the Hermes proxy (`hermes/…`)**

`nous/Hermes-4-405B` calls the Nous inference API at `NOUS_API_BASE`
(default `https://inference-api.nousresearch.com/v1`) with `NOUS_API_KEY`.
`hermes/Hermes-4-70B` calls a local `hermes proxy start` endpoint at
`HERMES_PROXY_BASE` (default `http://127.0.0.1:8645/v1`); the proxy accepts any
bearer and attaches your Nous Portal OAuth credential itself. The proxy is
**not** started by this pipeline. Both are treated as metered, and both are
billed to the `nous` ledger provider by default (`HERMES_PROXY_PROVIDER`).

**Spend caps (every metered entry).** Before each metered call the worst case —
prompt tokens plus `METERED_MAX_OUTPUT_TOKENS` of output — is priced and checked
against a persistent ledger (`COST_LEDGER_PATH`, default
`previous_run_results/_cost_ledger.json`). `METERED_USD_CAP` (default `$10`) caps
all metered providers combined; `PROVIDER_USD_CAPS='{"openai":10,"nous":5}'`
adds per-provider caps. A call that would exceed a cap is refused **without being
made** and the chain moves to the next entry (e.g. `openai` → `nous`); when every
metered entry is capped the run stops with `BudgetExhausted` (finished work kept,
re-run resumes). `claude-cli/…` calls are recorded at $0 and never blocked. An
unreadable ledger refuses metered calls rather than assuming nothing was spent.
Delete or re-point the ledger to start a fresh budget. Not covered: the vision
tier (`VLM_MODEL`), which still bills outside the ledger.

**3. Local models (`ollama/…`, `lm_studio/…`) — privacy option, off by default**

```
ALLOW_LOCAL_MODELS=true
LLM_MODELS=["ollama/llama3.1", "claude-cli/opus"]
```

Requires Ollama or LM Studio installed and serving. **Use this when the content
must not leave the machine** — private recordings, client material, anything
under NDA. Both the transcription and summarization steps then run entirely
locally, so a full run touches the network only to fetch the source video.

It is gated behind `ALLOW_LOCAL_MODELS` deliberately: local models need no API
key, so without the gate a misconfigured chain could silently fall through to a
much weaker model and emit notes that *look* fine. Expect noticeably shallower
analysis than the hosted models — `summarize_prompt.txt` was tuned and validated
against frontier models, and that validation does not carry over.

#### Measured model comparison

Same transcript, same prompt, tools disabled, summarization step only:

| Model | Time | Output | What you get |
|---|---|---|---|
| `haiku` | 16s | 3.3 KB | Accurate and faithful. Restates what was said, correct section discipline, little added analysis. |
| `sonnet` | 35s | 5.5 KB | Balanced. Adds domain framing and some nuance. |
| `opus` | 54s | 9.1 KB | Different in kind, not just longer — cites timestamp ranges, derives operational consequences the transcript never states, and explicitly names what is *absent* from the source. |

All three produced the same 8 sections and the same 3 correct `- None` answers,
so structural compliance is not the differentiator — analytical depth is. Use
`haiku` for bulk or playlist runs where coverage beats insight; `opus` for
material you actually intend to study.

#### Why tools are disabled on the CLI backend

`claude -p` is an **agent**, not a plain completion endpoint — by default it can
load skills, search the web, and read files. Left enabled, this backend:

- **Breaks grounding.** Observed live: summarizing a transcript about Claude
  models, the agent loaded API documentation and wrote confident,
  documentation-sourced claims into "Fact-Checked Notes" — a section the prompt
  deliberately instructs the model to *hedge* precisely because it is supposed to
  have no external lookup (see [Summarization behavior](#summarization-behavior)).
  Verified and unverified claims become indistinguishable.
- **Makes runs non-reproducible**, since output depends on what the agent chose
  to look up that time.
- **Opens a prompt-injection surface.** Transcripts are untrusted third-party
  content. An agent with file and shell access, summarizing text an attacker
  controls, is a bad combination.

So the CLI backend always passes `--disallowedTools` and runs from a scratch
working directory (a repo cwd would pull that repo's `CLAUDE.md` into an
unrelated summarization job). Both are configurable but neither should be
removed.

### Quick Start
1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd video2mdnotes
    ```

2.  **Create Environment File:**
    Copy the `.env.example` file to `.env` and add your API key.
    ```bash
    cp .env.example .env
    # Now, edit .env and add your key:
    # OPENAI_API_KEY="sk-..."
    ```

3.  **Build the Docker Container:**
    This command builds the application image. You only need to run this once, or whenever you change the Python code.
    ```bash
    docker compose build
    ```

## How to Run

You can run the application in three ways: as a CLI tool, as an API server, or interactively.

### 1. CLI Mode (One-off Process)
This is the simplest way to process a single video or playlist.
```bash
docker compose run --rm app "YOUR_VIDEO_OR_PLAYLIST_URL"
```
*   **`--rm`**: Automatically removes the container after the job finishes.
*   **Options**: You can pass flags like `--no-keep-wav` after the URL.

### 2. API Mode (Continuous Server)
Run the application as a REST API with a Swagger UI.
```bash
docker compose up api
```
*   **Access**: Open your browser to **http://127.0.0.1:8000/docs**.
*   **Usage**: Use the `POST /process` endpoint to submit URLs.
*   **Shutdown**: Press `Ctrl+C` in the terminal to gracefully stop the server.

### 3. Interactive Mode (Shell)
Start a shell inside the container to run commands manually.
```bash
docker compose run --rm app /bin/bash
```
*   **Inside the container**: You can run `python -m video2mdnotes.main "URL"` repeatedly.
*   **Exit**: Type `exit` to leave the container.

### Expected Output
After a successful run, a new directory will be created in `previous_run_results/` containing the final summary, the raw transcript, the original URL, and (optionally) the downloaded audio file.

### Transcript sourcing (captions-first)

Before downloading any audio, the pipeline probes the source for an existing
**human-authored** caption track. If it finds a usable one, it uses that and
skips both the audio download and the Whisper run — on a ~19-minute video that
is roughly ten minutes of work avoided. Otherwise it downloads audio and
transcribes locally, exactly as before.

Every transcript records where it came from, in its own front matter:

```yaml
transcript_source: captions (manual, en)   # or: whisper
```

ASR output and a human-authored transcript are not interchangeable evidence, so
this is recorded rather than left to be inferred from the model field.

**Machine captions are never used.** yt-dlp separates human-authored `subtitles`
from machine `automatic_captions`, and only the former are accepted. Auto-captions
are ASR *without* the vocabulary hint `build_initial_prompt()` supplies, so using
them would reintroduce the exact error class that hint exists to fix
("Claude" → "Cloud"). They are also silently machine-translated — a probe of one
English video listed `ab` (Abkhazian) among its automatic captions. Local Whisper
is the better fallback.

**A caption track must prove it carries speech.** Music videos return tracks that
are entirely `[♪♪♪]` / `[Music]` cues; those pass a naive "are there segments?"
check and would feed the summarizer garbage it is then obliged to summarize. A
track yielding fewer than `CAPTIONS_MIN_WORDS` word-equivalents is rejected in
favour of Whisper. Erring this way only ever costs time, never correctness.

**Language selection.** `CAPTIONS_LANG` is an ordered, comma-separated
preference — `en,ja` tries English then Japanese. The token `any` accepts
whatever manual track the source ships, which is how content in an unanticipated
language stays reachable on an otherwise English install:

```
CAPTIONS_LANG=en,any
```

Each entry matches on language prefix, so `en` accepts `en-US`/`en-GB`, with an
exact match preferred. Empty falls back to `FW_LANG`. It is kept separate from
`FW_LANG` because that setting tells Whisper what to expect, whereas a source in
another language can still be worth taking captions from.

Japanese, Chinese and Korean are **not** space-delimited, so the speech guard
counts CJK characters directly (roughly two characters per word-equivalent)
rather than counting spaced words — otherwise every CJK track would score zero
and be rejected as "no speech."

Disable the whole behavior with `CAPTIONS_FIRST=false` to always transcribe
locally.

### ASR backends (`core/asr/`)

`core/asr/` is a pluggable engine interface: `get_backend("faster-whisper:distil-large-v3")`,
`"parakeet-mlx"`, `"mlx-whisper"`. Each engine transcribes one bounded wav window into the same
`Segment`/`TranscriptResult` shape. A missing package (the MLX engines need the `asr-mlx` extra,
Apple Silicon only) makes the backend report itself unavailable instead of failing at import.
**The production path (`transcribe_audio`) still uses faster-whisper. WP-6b wires the interface in.**

Measured on two 10-minute CCA-F windows (`design/ASR_BENCHMARK.md`): parakeet decodes at 40× real
time, against 1.1–1.8× for faster-whisper large-v3 on CPU. Its content-word accuracy is about the
same. It mishears more domain terms ("Cloud Code", "STK"), because it takes no vocabulary prompt.
mlx-whisper looped and hallucinated, so it is not recommended for unattended runs. Re-run with
`scripts/bench_asr.py`. It extracts one window with ffmpeg and deletes it afterwards.

### Long runs: resuming and running out of capacity

A playlist of long videos is many large prompts, so a research run can exhaust
your subscription partway through. Two behaviors exist for that.

**Finished work is never redone.** Any video that already has a summary on disk
is skipped, matched on title independent of run date. Re-running the same
command continues where it stopped:

```
[1/10] Skipping (already summarized): But what is a neural network?...
[5/10] Processing: Large Language Models explained briefly
```

Pass `--force` to re-summarize everything anyway — e.g. to redo a playlist with
a better model.

**Running out of capacity does not silently start costing money.** When the
subscription backend is exhausted, the chain does **not** fall through to a paid
backend by default. `--on-exhaustion` controls what happens instead:

| Mode | Behavior |
|---|---|
| `wait` *(default)* | Alert, sleep until the limit resets, retry the same video. Uses a reset time from the CLI when it reports one, else `EXHAUSTION_WAIT_SECONDS`. |
| `metered` | Continue on the paid backends in `LLM_MODELS`, within the spend caps. |
| `local` | Continue on local backends only (needs `ALLOW_LOCAL_MODELS`). Never reaches a paid backend. |
| `stop` | Stop cleanly. Finished videos are kept and skipped on resume. |
| `ask` | Prompt once, then apply that answer for the rest of the run. |

`wait` is the default deliberately. An unattended run — a research agent that
cannot answer a prompt — must not begin spending money merely because nobody was
there to say no; it waits for capacity already paid for. `ask` degrades to
`wait`, not to spending, when there is no TTY.

`ALLOW_METERED_FALLBACK=true` lets an exhausted run continue on the metered
entries without changing `ON_EXHAUSTION` (it does not override `local`). Those
calls are bounded by the spend caps above; once the caps are spent too, the run
falls back to `ON_EXHAUSTION` (by default: wait for the reset). With it `false`
(the default), behaviour is exactly the table above.

Exhaustion is also distinguished from ordinary failure: a crashed backend still
fails over normally, and one bad video no longer abandons the rest of a
playlist. Every run ends with a ledger of summarized / skipped / failed, and
names the video it stopped on.

### Non-YouTube sources: pages that embed video

yt-dlp handles many sites directly. When it cannot handle a URL at all, the
pipeline fetches the page once and probes any embedded video URLs it finds,
rather than giving up:

```
$ ... main.py https://anthropic.skilljar.com/claude-code-101
ERROR: Unsupported URL: https://anthropic.skilljar.com/claude-code-101
yt-dlp cannot handle that URL directly; looking for embedded videos.
Found 8 embedded video(s) — processing each.
```

This exists because yt-dlp's generic extractor only sees markup present in the
served HTML. Course platforms commonly inject the player with JavaScript, so a
page whose lessons are perfectly ordinary public YouTube videos still reports
"Unsupported URL". Recognised: YouTube (`/embed/`, `youtu.be`, `watch?v=`,
`youtube-nocookie`) and Vimeo. Results are de-duplicated, kept in document order
(usually lesson order), and capped at 25 per page.

**What this is not.** It does not log in, execute JavaScript, or work around any
access control. It performs one ordinary anonymous fetch — if a page does not
serve embed URLs to an anonymous visitor, it yields nothing and the original
yt-dlp error stands. Content behind a login stays behind that login.

Because this widens the set of third-party URLs reaching the fetcher, the same
restrictions as caption fetching apply, in shared code (`core/webfetch.py`):
**http/https only** — `urllib` will otherwise open `file://` and read local files
into a transcript — and a hard size cap. Disable the whole behavior with
`SCRAPE_PAGE_EMBEDS=false`.

Note that embedded course video often has **auto-captions only**, in which case
captions-first correctly declines and Whisper runs. Captions-first's hit rate
depends entirely on whether the publisher writes real captions.

### Visual content: video that teaches on screen

Some instructional video carries its substance in the pixels — slides, code
screencasts, tables, UI walkthroughs, animated explainers. The audio pipeline
cannot see any of it; on a narration-free product video it correctly reports
"No speech detected" while the actual content sits on screen.

Enable with `EXTRACT_FRAMES=true`. It is **off by default because the second
tier costs money.**

#### How it works — two tiers with very different economics

1. **Scene detection** (`ffmpeg`) samples a frame only when the picture
   materially changes, then a perceptual hash drops near-duplicates. This is
   what makes the rest affordable: a 19-minute video becomes ~24 distinct
   images, not thousands of frames.
2. **Local OCR** reads every keyframe. Free, offline, no tokens.
3. **A vision model** is called *only* for frames whose OCR yields less than
   `VLM_ESCALATE_BELOW_WORDS` real words — i.e. the picture, not the text, is
   carrying the meaning. Capped at `VLM_MAX_FRAMES` per video.

Results land in the summary as a `## Visual Content` section with embedded,
relative-linked images under `frames/`, so the run directory stays portable.
OCR text and model descriptions are labelled separately and carry a
`_source:_` line — a machine reading of on-screen text and a model's
interpretation of a diagram are different kinds of evidence.

#### ⚠️ Local OCR is macOS-only

OCR uses the **macOS Vision framework** (`pip install -e '.[ocr]'`). It is free,
fast, and good on code and UI — but it does not exist on Linux or Windows.

**On other platforms every keyframe escalates to the vision model**, turning a
near-free feature into metered spend per frame. The pipeline warns loudly when
this happens. Non-macOS collaborators should either leave `EXTRACT_FRAMES=false`,
set `VLM_ENABLED=false` (frames extracted, nothing read), or accept the cost.
Substituting Tesseract or PaddleOCR would restore the free tier; neither is
wired up.

#### Cost

The vision tier **cannot use the `claude-cli` subscription backend** — that runs
with `Read` disabled as the prompt-injection guard, and images cannot be passed
without reopening it. Vision is always metered API.

Measured on a 19-minute animated explainer (24 keyframes after de-duplication,
all of which escalated because the content is diagrams rather than text):

| `VLM_MODEL` | Cost for that video |
|---|---|
| `anthropic/claude-haiku-4-5` | ~$0.11 |
| `anthropic/claude-opus-4-8` | ~$0.57 |

`VLM_MAX_FRAMES=15` caps it further. **Savings depend entirely on content:** a
slide deck of prose is mostly handled by free OCR, while a diagram-heavy
explainer escalates nearly everything — that measured run was the worst case.

### Summarization behavior
`summarize_prompt.txt` is domain-adaptive: it detects whether the source content is
technical (programming/ML/data/architecture) or Aikido/martial arts, and interprets each
output section accordingly (e.g. "New Commands / Techniques" means CLI commands for
technical content, named techniques/drills for Aikido). It also includes a "Sponsor / Ad
Content" section that separates podcast sponsor reads from editorial content instead of
mixing them in or dropping them, grounding instructions against fabricating content for
thin/empty transcripts, and confidence-calibrated fact-checking (the summarizer has no web
search, so it's asked to flag its own uncertainty rather than assert flatly). Ported from
base_human_learn_sys and validated 2026-08-08 against real technical (AI Daily Brief
podcast) and Aikido content.

### Long recordings (long-form path)

A transcript over `DEFAULT_MAX_WORDS` (12,000 words) no longer goes through the
single-call prompt above — a 12-hour course is ~120k words. `process` and
`media` route it to `core/longform.py` automatically (the short path is
unchanged), and `longform` runs it on an **existing** run dir with no download:

```bash
# What would run: chapters, parts, passages, estimated calls. No model calls.
python -m video2mdnotes.main longform previous_run_results/<run> --plan-only
# Pilot on a few chapters (numbers or title substrings), into a scratch dir
python -m video2mdnotes.main longform previous_run_results/<run> \
    --only "1,Sessions management,7" --out /tmp/pilot
# Everything; chapters in parallel (parts of one chapter stay sequential)
python -m video2mdnotes.main longform previous_run_results/<run> --concurrency 3
```

What it does, in order: reads `source_meta.json` (or derives it from
`yt_info.json`), labels the transcript's provenance, writes a glossary-corrected
copy plus `corrections.json` (the raw transcript is never edited), cuts the
corrected transcript with the structure ladder (chapters → description list →
cues → windows; oversize chapters split into parts), then makes **one model
call per passage**. Each passage prompt carries the dated source header, the
course skeleton, every caption line with its `[H:MM:SS]`, a mishearing hint,
the uncertainty and time-sensitive instructions; part 2+ of a split chapter
also gets the previous part's **ledger** (concepts, corrections, open threads,
last point — capped at 800 words, oldest dropped first) and its last ~150 s as
`CONTEXT ONLY`. Prompts: `prompts/longform/` + `prompts/fragments/`. Exam or
certification metadata selects the `exam-prep` section profile
(`core/domains.py`), which adds "Exam-relevant points".

**Resumable.** Each finished passage is written to
`chunk_summaries/<NN>_<slug>_p<i>.md` and `ledger/<NN>_p<i>.json`, with status in
`longform_state.json`. A rerun skips them and restores the ledger, so a
`SubscriptionExhausted` / `BudgetExhausted` stop loses nothing (`--no-resume`
redoes everything).

**Citation audit (no model call).** Before a passage is checkpointed,
`core/citations.py` checks every cited bullet and table row: its salient
terms (code spans, identifiers, tool/command names, numbers with units, plus
words that are rare in the passage) must be said within ±60 s of the cited
time. A citation whose terms are only said at ONE other place in the passage
is moved there; one whose strong terms are never said is marked
`_(unverified citation)_`. Every decision is logged in `citation_audit.json`
(`citation:retarget` / `citation:unverified`, plus `citation:ambiguous`
entries that changed nothing). `longform.reaudit_chunks(run_dir)` re-audits
existing `chunk_summaries/` without calling a model. Ledger entries carry
`status: observed|expected|superseded`, so a demo's expected outcome is not
handed to the next part as fact.

**Assembly never re-summarizes.** The final
`summaries/<name>.summary.md` is frontmatter (`published`, `as_of`,
provenance, structure rung, model, run id) + a `## Course Map` (the only later
model call: one-line gists per chapter, batched six passages per call; chapter
and time-range columns come from the skeleton, and a chatty or failed reply
falls back to a deterministic table) + one `## <chapter> [H:MM:SS–H:MM:SS]`
section per chapter (`### Part i/n` for split ones) + merged
`## Transcription Uncertainty`, `## Time-Sensitive Claims` (every row dated)
and `## Fact-Checked Notes`. The notes are then linted against the same
glossary; residual mishearings are fixed and logged in `corrections.json` with
rule `notes:<rule>`. The full transcript is **not** appended (it is in
`transcripts/`). Score a run with `scripts/eval_longform.py` (below).

## Development
If you want to modify the code or run tests locally, you'll need a local Python environment.

### Local Setup
1.  **Install Python**: We recommend Python 3.11.
2.  **Install uv**:
    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```
3.  **Create Virtual Environment & Install Dependencies**:
    This command reads the `pyproject.toml` and installs all production and development dependencies.
    ```bash
    uv sync --with-dev
    ```
4.  **Activate Virtual Environment**:
    ```bash
    source .venv/bin/activate
    ```

### Development Tools
Once your local environment is set up, you can use these commands.

**Run Tests:**
```bash
uv run pytest
```

**Run Linter & Formatter:**
This project uses `ruff` to check for errors and format the code.
```bash
# Check for errors
uv run ruff check .

# Automatically fix fixable errors
uv run ruff check . --fix

# Format all files
uv run ruff format .
```

### Long-form eval harness (P1–P8)

`scripts/eval_longform.py` scores a long-form run directory against the pass
criteria in `design/LONGFORM_EXECUTION_PLAN.md`. It is deterministic and calls
no model. The run-directory contract it reads (`summaries/`, `transcripts/`,
`source_meta.json`, `corrections.json`, the `## Time-Sensitive Claims` table) is
in the docstring of `src/video2mdnotes/eval/longform.py`.

```bash
.venv/bin/python scripts/eval_longform.py <run_dir> \
    --expected-chapters tests/fixtures/eval_longform/cca_f_expected_chapters.txt
```

It writes `eval_report.json`, `eval_report.md` and `p8_sample.json` to
`<run_dir>/eval/` (or `--out`), and exits 0 only if P1–P7 all pass. P8 is
recorded as "pending reviewer": `p8_sample.json` holds 20 seeded, cited claim
bullets with ±60 s of transcript each, for an independent reviewer agent to
judge. `--criteria P3` runs a subset (the report is marked partial);
`--scan-transcript` also counts glossary mishearings in the transcript. The
glossary (`src/video2mdnotes/eval/glossary_default.json`) lists explicit wrong
forms only, so "cloud provider" and "cloud computing" are never flagged.

---

## Keeping the docs true

**Update this readme after any session that changes behaviour — not just
setup.** A readme that describes the previous version is worse than none,
because it is trusted.

Companion docs:

| File | For |
|---|---|
| `AGENTS.md` | rules for anyone (human or agent) changing this code |
| `HUMAN_PLANS.md` | context for picking the work back up |
| `FIXED_BUGS.md` | 30 entries; most share one shape — confident, plausible, wrong |
| `design/` | the work-package designs and methodology writeups |

ADRs for this pipeline currently live in `agent_set_up/ADR_INSTALL_LOG.md`
rather than here.
