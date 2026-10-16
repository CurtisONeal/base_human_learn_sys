import datetime as dt
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from pydantic import BaseModel
import litellm

from video2mdnotes.config import settings
from video2mdnotes.core import costs
from video2mdnotes.core.costs import BudgetExhausted  # noqa: F401 - re-exported
from video2mdnotes.core.transcriber import TranscriptResult


class SummaryResult(BaseModel):
    """Represents the result of an LLM summary generation."""
    source_file: Path
    model_name: str
    summary_text: str
    generated_at: dt.datetime


CLAUDE_CLI_PREFIX = "claude-cli/"

# Signatures that mean "this account is out of capacity for now", as opposed to
# an ordinary failure. The distinction matters: an ordinary failure should fall
# through to the next backend, but exhaustion must NOT silently escalate to
# metered billing — an unattended research run would spend real money without
# anyone choosing to.
_EXHAUSTION = re.compile(
    r"rate.?limit|usage limit|limit reached|out of (?:usage|credit)|quota|"
    r"too many requests|\b429\b|exceeded your|upgrade to continue",
    re.IGNORECASE,
)

# Best-effort reset-time parsing. The CLI does not guarantee any of these, so
# every branch is optional and the caller falls back to a configured default.
_RESET_ABSOLUTE = re.compile(
    r"resets?\s+at\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)", re.I
)
# Durations are matched independently rather than anchored to a keyword: an
# earlier version keyed off "again|after|in" and "try again in 3 hours" matched
# on "again", then found no digits before "in" and silently gave up.
_RESET_HOURS = re.compile(r"(\d+)\s*(?:hours?|hrs?|h)\b", re.I)
_RESET_MINUTES = re.compile(r"(\d+)\s*(?:minutes?|mins?|m)\b", re.I)


class SubscriptionExhausted(Exception):
    """The subscription backend is out of capacity (not a generic failure)."""

    def __init__(self, message: str, reset_at: dt.datetime | None = None):
        super().__init__(message)
        self.reset_at = reset_at


def parse_reset_time(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """Extract a limit-reset time from CLI error output, if it reports one."""
    now = now or dt.datetime.now()

    text = text or ""

    absolute = _RESET_ABSOLUTE.search(text)
    if absolute:
        try:
            return dt.datetime.fromisoformat(absolute.group(1).replace(" ", "T"))
        except ValueError:
            pass

    hours = _RESET_HOURS.search(text)
    minutes = _RESET_MINUTES.search(text)
    if hours or minutes:
        delta = dt.timedelta(
            hours=int(hours.group(1)) if hours else 0,
            minutes=int(minutes.group(1)) if minutes else 0,
        )
        if delta:
            return now + delta
    return None

# litellm providers that run locally / self-hosted and therefore need no API key.
LOCAL_PROVIDERS = frozenset({
    "ollama", "ollama_chat", "lm_studio", "vllm", "hosted_vllm",
    "llamafile", "xinference", "openai_like",
})


def _provider_of(model: str) -> str:
    """The provider prefix of a chain entry ('' when the entry is bare)."""
    return model.split("/", 1)[0].strip().lower() if "/" in model else ""


# .env.example ships these as literal placeholders. They are truthy, so a bare
# `if not key` check passes them straight through to the provider and the run
# fails with a confusing auth error instead of "no key configured".
_PLACEHOLDER_KEYS = frozenset({"your_key_here", "sk-...", "changeme", ""})


def real_key(value: str | None) -> str | None:
    """A configured key, or None if it is absent or a placeholder."""
    value = (value or "").strip()
    return None if value in _PLACEHOLDER_KEYS else value


def _api_key_for(model: str) -> str | None:
    """The configured key for a litellm entry, by provider prefix."""
    return real_key({
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }.get(_provider_of(model)))


def _complete_claude_cli(model: str, system_prompt_file: Path, user_message: str) -> str:
    """Summarize via the `claude` CLI on subscription billing (no API key).

    Two guards here are load-bearing, not tuning:

    * API keys are stripped from the child environment. With them set, the CLI
      bills the metered API instead of the subscription — the same reason
      worker.sh unsets them.
    * Tools are disabled and the child runs in a scratch cwd. `claude -p` is an
      agent: left alone it will load skills and read files, which (a) breaks the
      no-external-lookup premise the summarize prompt is written against, and
      (b) hands file/shell access to an agent processing untrusted transcript
      text. A repo cwd would also pull that repo's CLAUDE.md into the job.
    """
    alias = model[len(CLAUDE_CLI_PREFIX):].strip()
    if not alias:
        raise ValueError(f"No model alias in {model!r} (expected e.g. claude-cli/opus)")

    binary = shutil.which(settings.claude_cli_path) or settings.claude_cli_path

    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}

    with tempfile.TemporaryDirectory(prefix="v2mdnotes-summary-") as neutral_cwd:
        proc = subprocess.run(
            [
                binary, "-p",
                "--model", alias,
                "--system-prompt-file", str(system_prompt_file),
                "--disallowedTools", settings.claude_cli_disallowed_tools,
            ],
            input=user_message,
            capture_output=True,
            text=True,
            env=env,
            cwd=neutral_cwd,
            timeout=settings.claude_cli_timeout,
        )

    if proc.returncode != 0:
        combined = f"{proc.stderr or ''}\n{proc.stdout or ''}"
        detail = combined.strip()[:400]
        if _EXHAUSTION.search(combined):
            raise SubscriptionExhausted(
                f"claude CLI subscription exhausted: {detail}",
                reset_at=parse_reset_time(combined),
            )
        raise RuntimeError(f"claude CLI exited {proc.returncode}: {detail}")

    content = (proc.stdout or "").strip()
    if not content:
        raise ValueError("claude CLI returned an empty summary.")
    return content


def _complete(model: str, api_key: str | None, system_prompt: str, user_message: str,
              **extra) -> str:
    """Single litellm call; raises on empty output so the caller can fall back."""
    return _complete_response(model, api_key, system_prompt, user_message, **extra)[0]


def _complete_response(model: str, api_key: str | None, system_prompt: str,
                       user_message: str, **extra) -> tuple[str, object]:
    """litellm call returning (content, raw response) — the response carries usage."""
    response = litellm.completion(
        model=model,
        api_key=api_key,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        timeout=300,  # 5 minutes
        **{k: v for k, v in extra.items() if v is not None},
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("LLM returned an empty summary.")
    return content, response


# OpenAI-compatible providers addressed by their own prefix. litellm has no
# native "nous"/"hermes" provider, so these are rewritten to its generic
# openai/ adapter with an explicit api_base — the idea from the June
# LLM_API_BASE branch (4406b6d), but per entry, so OpenAI proper and Nous can
# both sit in one chain with separate keys and separate caps. A single global
# api_base (as that branch had) would also have redirected openai/gpt-4o.
OPENAI_COMPATIBLE_PROVIDERS = frozenset({"nous", "hermes"})

# Any bearer works: the Hermes proxy discards it and attaches the real OAuth
# credential. Non-empty because the openai client refuses an empty key.
HERMES_PROXY_DUMMY_KEY = "hermes-proxy"


@dataclass(frozen=True)
class Route:
    """How one chain entry is called and how it is accounted for."""
    entry: str                 # as written in LLM_MODELS
    kind: str                  # costs.SUBSCRIPTION | costs.METERED | costs.LOCAL
    ledger_provider: str       # name used in the ledger and PROVIDER_USD_CAPS
    litellm_model: str | None  # model string handed to litellm (None for CLI)
    api_base: str | None = None
    api_key: str | None = None


def resolve_route(entry: str) -> Route:
    """Map an LLM_MODELS entry to its backend, endpoint, key and ledger name."""
    provider = _provider_of(entry)
    if entry.startswith(CLAUDE_CLI_PREFIX):
        return Route(entry, costs.SUBSCRIPTION, "claude-cli", None)
    if provider in LOCAL_PROVIDERS:
        return Route(entry, costs.LOCAL, provider, entry)
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        name = entry.split("/", 1)[1].strip()
        if provider == "nous":
            return Route(entry, costs.METERED, "nous", f"openai/{name}",
                         api_base=settings.nous_api_base,
                         api_key=real_key(settings.nous_api_key))
        return Route(entry, costs.METERED,
                     (settings.hermes_proxy_provider or "hermes").strip().lower(),
                     f"openai/{name}", api_base=settings.hermes_proxy_base,
                     api_key=HERMES_PROXY_DUMMY_KEY)
    return Route(entry, costs.METERED, provider or "unknown", entry,
                 api_key=_api_key_for(entry))


def _complete_metered(route: Route, system_prompt: str, user_message: str) -> str:
    """Cap-checked metered call: estimate, refuse if over a cap, call, record.

    Raises BudgetExhausted BEFORE any network call when the worst case
    (prompt tokens + the full output allowance) would exceed a cap.
    """
    ledger = costs.get_ledger()
    max_out = settings.metered_max_output_tokens
    prompt_tokens = costs.estimate_prompt_tokens(
        system_prompt + "\n" + user_message, route.litellm_model
    )
    estimate = costs.price_tokens(route.entry, prompt_tokens, max_out, route.litellm_model)
    ledger.check(route.ledger_provider, estimate,
                 settings.metered_usd_cap, settings.provider_usd_caps)

    content, response = _complete_response(
        route.litellm_model, route.api_key, system_prompt, user_message,
        api_base=route.api_base, max_tokens=max_out,
    )
    tin, tout, usd, estimated = costs.usage_and_cost(
        response, route.entry, route.litellm_model, fallback_usd=estimate
    )
    try:
        ledger.record(provider=route.ledger_provider, model=route.entry,
                      kind=costs.METERED, input_tokens=tin, output_tokens=tout,
                      usd=usd, estimated=estimated)
    except Exception as exc:  # noqa: BLE001 - the call is paid for; keep its output
        print(f"Summarizer: WARNING — could not record ${usd:.4f} for {route.entry} "
              f"in the cost ledger ({exc}). Spend caps are now under-counting.")
    return content


def _record_unmetered(route: Route, system_prompt: str, user_message: str,
                      output: str) -> None:
    """Count a subscription/local call in the ledger at $0 (never blocks)."""
    try:
        costs.get_ledger().record(
            provider=route.ledger_provider, model=route.entry, kind=route.kind,
            input_tokens=(len(system_prompt) + len(user_message)) // 4,
            output_tokens=len(output) // 4, usd=0.0, estimated=True,
        )
    except Exception as exc:  # noqa: BLE001 - accounting must not fail a free call
        print(f"Summarizer: could not record {route.entry} in the cost ledger ({exc}).")


EMPTY_TRANSCRIPT_PLACEHOLDER = (
    "No speech detected in this video — transcript was empty "
    "(music/text-only or silent content)."
)


def generate_summary(transcript: TranscriptResult) -> SummaryResult:
    """
    Generates a summary for a given transcript using an LLM.

    Backends are tried in settings.llm_models order. That list is a FAILOVER
    chain, not a quality ladder — entry 1 does essentially all the work and
    later entries fire only when an earlier one errors. The full raw transcript
    is appended to the end of the summary.

    If the transcript has no segments (e.g. a narration-free/music-only or silent
    source video), the LLM is not called at all — calling it on empty input causes
    it to fabricate a plausible-looking but entirely invented summary. A clear
    placeholder is returned instead.

    Raises:
        FileNotFoundError: If the prompt template file is not found.
        RuntimeError: If no configured provider could produce a summary.
    """
    if not transcript.segments:
        return SummaryResult(
            source_file=transcript.source_file,
            model_name="none",
            summary_text=EMPTY_TRANSCRIPT_PLACEHOLDER,
            generated_at=dt.datetime.now(),
        )

    if not settings.prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {settings.prompt_file}")

    # 1. Load the system prompt from the template file
    system_prompt = settings.prompt_file.read_text(encoding="utf-8")

    # 2. Prepare the user message (the full transcript)
    user_message = transcript.markdown_content

    llm_output, model = complete_with_failover(system_prompt, user_message)

    # Append the full transcript (raw full_text, no timestamps) to the summary.
    return SummaryResult(
        source_file=transcript.source_file,
        model_name=model,
        summary_text=f"{llm_output}\n\n## Transcript\n{transcript.full_text}",
        generated_at=dt.datetime.now(),
    )


def complete_with_failover(system_prompt: str, user_message: str) -> tuple[str, str]:
    """Run one completion through the failover chain. Returns (output, model).

    Factored out of `generate_summary` so book chunking can reuse the exact
    same exhaustion and no-surprise-spend behaviour. A second copy of this loop
    is precisely the kind of thing that would grow its own subtly different
    escalation policy — and the policy here is the one that keeps an unattended
    overnight run from quietly starting to spend money.

    Every metered entry is cap-checked against the cost ledger before it is
    called (core/costs.py). A capped entry is skipped like a failed one, so the
    chain moves on (e.g. openai -> nous).

    Raises:
        SubscriptionExhausted: re-raised so the caller can wait and retry —
            also when metered fallback was allowed but every paid entry was
            capped or failed.
        BudgetExhausted: no entry succeeded and at least one was refused by a
            spend cap. Callers stop the run cleanly (finished work is kept).
        RuntimeError: every configured provider failed.
    """
    errors: list[str] = []
    exhausted = False
    exhaustion: SubscriptionExhausted | None = None
    budget_blocks: list[BudgetExhausted] = []
    policy = (settings.on_exhaustion or "wait").strip().lower()
    # ALLOW_METERED_FALLBACK widens a non-"local" policy to the metered
    # entries; it never overrides an explicit "local" (no paid backdoor there).
    metered_after_exhaustion = policy == "metered" or (
        settings.allow_metered_fallback and policy != "local"
    )

    for model in settings.llm_models:
        route = resolve_route(model)
        provider = _provider_of(model) or "unknown"

        # Once the subscription is exhausted, what may still run depends on
        # policy. The default deliberately refuses to reach a paid backend:
        # an unattended run must not start spending because nobody was there
        # to answer. main.py turns the raised exception into a wait or a stop.
        if exhausted and policy == "local":
            if provider not in LOCAL_PROVIDERS:
                errors.append(f"{model}: skipped (on-exhaustion=local allows local backends only)")
                continue
        elif exhausted and not metered_after_exhaustion:
            break

        try:
            if route.kind == costs.SUBSCRIPTION:
                # Subscription CLI: authenticates from the keychain, no API key.
                # The CLI takes a system prompt as a *file*, so the caller's
                # prompt is written to one. Passing settings.prompt_file
                # directly would silently ignore the argument — which is
                # exactly what happened when book chunks were being summarized
                # with the video prompt.
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".txt", prefix="v2mdnotes-sys-",
                    encoding="utf-8", delete=False,
                ) as handle:
                    handle.write(system_prompt)
                    prompt_path = Path(handle.name)
                try:
                    llm_output = _complete_claude_cli(model, prompt_path, user_message)
                finally:
                    prompt_path.unlink(missing_ok=True)
                _record_unmetered(route, system_prompt, user_message, llm_output)
            elif route.kind == costs.LOCAL:
                # Local models need no key, so nothing except this gate stops a
                # chain from silently falling through to a much weaker model.
                if not settings.allow_local_models:
                    errors.append(
                        f"{model}: local models disabled "
                        f"(set ALLOW_LOCAL_MODELS=true to enable)"
                    )
                    continue
                llm_output = _complete(model, None, system_prompt, user_message)
                _record_unmetered(route, system_prompt, user_message, llm_output)
            else:
                if not (route.api_key or "").strip():
                    errors.append(f"{model}: no API key configured")
                    continue
                llm_output = _complete_metered(route, system_prompt, user_message)
        except SubscriptionExhausted as e:
            exhausted = True
            exhaustion = e
            errors.append(f"{model}: {e}")
            if metered_after_exhaustion:
                print(f"Summarizer: {model} exhausted; continuing on metered backends "
                      f"(capped at ${settings.metered_usd_cap:.2f}).")
            else:
                print(f"Summarizer: {model} exhausted; not escalating to a paid backend "
                      f"unless --on-exhaustion or ALLOW_METERED_FALLBACK says to.")
            continue
        except BudgetExhausted as e:
            budget_blocks.append(e)
            errors.append(f"{model}: {e}")
            print(f"Summarizer: {model} refused by spend cap ({e}); trying next provider if any.")
            continue
        except Exception as e:  # noqa: BLE001 - record and try the next provider
            errors.append(f"{model}: {e}")
            print(f"Summarizer: {model} failed ({e}); trying next provider if any.")
            continue

        return llm_output, model

    # Exhaustion is re-raised as itself so the caller can wait for the limit to
    # reset and retry this same video, rather than treating it as a dead run.
    # This holds even when the paid entries were also capped: waiting for
    # capacity already paid for is the plan's answer to "everything is spent".
    if exhausted:
        raise exhaustion

    if budget_blocks:
        last = budget_blocks[-1]
        raise BudgetExhausted(
            "Stopping: every metered provider that could take this call is at its "
            "spend cap, and no other provider succeeded. Raise METERED_USD_CAP / "
            "PROVIDER_USD_CAPS or point COST_LEDGER_PATH at a new ledger to "
            "continue. Details: " + "; ".join(errors),
            provider=last.provider, spent=last.spent, cap=last.cap,
            estimate=last.estimate,
        )

    raise RuntimeError(
        "Summary generation failed for all configured providers: " + "; ".join(errors)
    )


# --- Documents ------------------------------------------------------------

# "Preserve code exactly" was too weak: measured on llmops, 140 fenced blocks
# went in and ZERO came out — the model described the code in prose instead of
# reproducing it. For a corpus that is mostly programming books, notes without
# code are notes about a book you still have to open. The instruction now says
# *copy*, not *preserve*, and says it twice.
_CHUNK_SYSTEM = """You are summarizing ONE PASSAGE of a longer book.

Rules:
- Summarize only what this passage says. Do not infer what the rest of the book covers.
- Every claim must carry a locator in brackets. Each section in the passage is
  preceded by its own locator, e.g. [ch. 2 §Four Goals for LLMOps] or
  [p. 214 §Indexing]. Use the locator of the SECTION the claim came from — NOT
  the passage-span locator at the top. A bare chapter number is not good
  enough: a reader must be able to find the claim without re-reading the
  chapter.
- If the passage is boilerplate (copyright, index, front matter), reply with exactly: SKIP

CODE — this matters most:
- The passage marks code with ``` fences. COPY every fenced block into your
  output inside its own ``` fence, character for character. Do not paraphrase
  it, do not describe it in prose, do not abbreviate it with "..." or comments
  like "# rest of implementation".
- Put one sentence before each block saying what it does, then the block.
- If a block is longer than about 40 lines, copy the part the passage's own
  discussion refers to and mark the omission explicitly.
- Never invent an identifier, import, flag or API name that is not in the passage.
"""

# Passages combined per reduce call. Six keeps each call small enough to finish
# inside the CLI timeout; a 16-passage reduce timed out at 900s and lost the
# whole book's work.
_REDUCE_BATCH = 6

_REDUCE_SYSTEM = """You are assembling per-passage notes for one book into a single set of notes.

Rules:
- Work only from the notes given. Add nothing that is not in them.
- Keep the locators. A reader must be able to find every claim in the book.
- Merge duplicates; preserve disagreements rather than resolving them silently.
- Structure: overview, then the main topics in the book's own order, then key terms.
- CODE BLOCKS PASS THROUGH VERBATIM. Copy every ``` fenced block from the notes
  into your output unchanged. Never summarize a code block into a sentence, and
  never drop one because a nearby passage covers the same topic — the reduce is
  where code silently disappears.
- Output ONLY the notes document. No preamble, no commentary on the input, no
  checklist of what you did, and no questions or offers of further work. The
  first line of your reply is the first line of the document.
- Be comprehensive. These notes replace the book for a reader who cannot open
  it, so do not compress away detail to be concise.
"""


# A conversational opener ("I'll assemble these notes into…") is harmless when
# a real document follows, and the good EPUB run began with exactly that. Only
# the preamble is dropped, never the body.
_CHAT_OPENER = re.compile(
    r"^\s*(i'?ll\b|here'?s\b|sure[,!]|certainly[,!]|this is (excellent|great|good))",
    re.IGNORECASE,
)

# Offers of further work only ever appear when the model thinks it is in a
# conversation rather than producing an artifact.
_CHAT_OFFER = re.compile(
    r"\bdo you want me to\b|\bwould you like me to\b|\bonce you confirm\b|"
    r"\bshall i\b|\blet me know if you\b",
    re.IGNORECASE,
)


def _strip_chat_preamble(text: str) -> str:
    """Drop a conversational lead-in that sits above the first heading."""
    lines = (text or "").strip().splitlines()
    first_heading = next(
        (i for i, line in enumerate(lines) if line.lstrip().startswith("#")), None
    )
    if first_heading:
        head = "\n".join(lines[:first_heading])
        if _CHAT_OPENER.search(head) and len(head.split()) < 80:
            return "\n".join(lines[first_heading:]).strip()
    return (text or "").strip()


def _reduce_collapsed(final: str, passage_notes: list) -> bool:
    """Whether the reduce returned chat or a fragment instead of the notes.

    Judged by PROPORTION, not phrasing. Wording is a poor signal — the run that
    produced a genuinely good 15,247-word document opened with "I'll assemble
    these chapter notes…", which any phrase-matching guard flags. Size relative
    to the input cannot be faked: a reduce of 16 passages that returns 177 words
    has lost the book regardless of how it is worded.
    """
    source_words = sum(len(n.split()) for n in passage_notes)
    final_words = len(final.split())
    if final_words < 300 and source_words > 1_000:
        return True
    if source_words and final_words < source_words * 0.10:
        return True
    return bool(_CHAT_OFFER.search(final)) and final_words < source_words * 0.35


def summarize_document(source, max_words: int = None, progress=None) -> SummaryResult:
    """Summarize a book-length `SourceText` by map-reduce over its chunks.

    Chunks are summarized independently and then reduced, because no useful
    model reads 136,000 words in one request. Two properties are load-bearing:

    * **Locators survive the reduce.** Each chunk carries exactly one chapter
      or page reference, so a claim in the final notes can be traced to a place
      in the book. A summary of a book you cannot check is worth very little.
    * **Exhaustion stops the whole document, not one chunk.** A book that is
      half summarized because the subscription ran out mid-way would look
      complete while silently omitting later chapters. The exception propagates
      so the caller can wait and resume.

    The raw text is deliberately NOT appended the way a transcript is: book text
    is copyrighted, and writing a whole book into an artifact that may land in a
    vault or a repo is not something to do by default.
    """
    from video2mdnotes.core.chunking import chunk_sections

    chunks = chunk_sections(source.sections, **(
        {"max_words": max_words} if max_words else {}))
    if not chunks:
        return SummaryResult(
            source_file=Path(source.origin), model_name="none",
            summary_text=EMPTY_TRANSCRIPT_PLACEHOLDER,
            generated_at=dt.datetime.now(),
        )

    notes: list[str] = []
    models: set[str] = set()
    for chunk in chunks:
        if progress:
            progress(chunk.index, chunk.total, chunk.locator)
        header = (
            f"Book: {source.title}\n"
            f"Passage {chunk.index} of {chunk.total} — locator [{chunk.locator}]\n\n"
        )
        output, model = complete_with_failover(
            _CHUNK_SYSTEM, header + chunk.to_prompt_text()
        )
        models.add(model)
        if output.strip().upper() != "SKIP":
            notes.append(f"### [{chunk.locator}] {' > '.join(chunk.heading_path)}\n{output.strip()}")

    if not notes:
        return SummaryResult(
            source_file=Path(source.origin), model_name=", ".join(sorted(models)) or "none",
            summary_text=EMPTY_TRANSCRIPT_PLACEHOLDER,
            generated_at=dt.datetime.now(),
        )

    # The per-passage notes are the expensive part — one model call each, and
    # for a book that is most of the run. Everything below is a *reduce* over
    # them, and a reduce failure must never discard them. The media path already
    # guarantees this for transcripts ("transcript survives a summarizer
    # failure"); a book that cost 16 calls deserves the same.
    passage_notes = list(notes)

    def _fallback(reason: str) -> str:
        return (
            f"# {source.title} — passage notes\n\n"
            f"> The per-passage notes below are complete. Combining them into a "
            f"single summary failed: {reason}\n"
            f"> Re-running will reuse nothing, so keep this file.\n\n"
            + "\n\n".join(passage_notes)
        )

    # Reduce in exactly ONE round of batches, then CONCATENATE the results.
    #
    # Reducing recursively — summarizing summaries until one call can hold
    # everything — is the obvious design and it fails twice over. It compounds
    # loss at every round, and the second round hands the model text that is
    # already assembled notes, which it reads as finished work: one real run
    # returned "This is excellent work ✓ Locators preserved… Do you want me to
    # merge Chapters 1–5?" — 177 words of chat in place of a book.
    #
    # Concatenation avoids both. Each batch covers a contiguous, ordered span of
    # the book and keeps its own locators, so joining them is coherent without a
    # second pass that can only lose detail.
    try:
        if len(notes) <= _REDUCE_BATCH:
            final, model = complete_with_failover(
                _REDUCE_SYSTEM, f"Book: {source.title}\n\n" + "\n\n".join(notes)
            )
            models.add(model)
        else:
            parts = []
            for i in range(0, len(notes), _REDUCE_BATCH):
                batch = notes[i:i + _REDUCE_BATCH]
                output, model = complete_with_failover(
                    _REDUCE_SYSTEM,
                    f"Book: {source.title}\n"
                    f"You are given passages {i + 1}-{i + len(batch)} of "
                    f"{len(notes)}; another pass covers the rest. Do NOT title "
                    f"the output after the passage numbers - that is internal "
                    f"batching a reader should never see. Use the topics.\n\n"
                    + "\n\n".join(batch),
                )
                models.add(model)
                parts.append(output.strip())
            final = _strip_chat_preamble("\n\n---\n\n".join(parts))

        final = _strip_chat_preamble(final)
        if _reduce_collapsed(final, passage_notes):
            final = _fallback(
                f"the reduce returned {len(final.split())} words from "
                f"{sum(len(n.split()) for n in passage_notes)} words of passage "
                f"notes — it chatted or truncated instead of assembling"
            )
    except (SubscriptionExhausted, BudgetExhausted):
        raise
    except Exception as exc:  # noqa: BLE001 - keep the work that succeeded
        final = _fallback(str(exc)[:200])

    provenance = (
        f"\n\n---\nsource: {source.origin}\n"
        f"provenance: {source.provenance}\n"
        f"structure_is_inferred: {source.structure_is_inferred}\n"
        f"passages: {len(chunks)}  words: {source.word_count:,}\n"
        f"models: {', '.join(sorted(models))}\n"
    )
    return SummaryResult(
        source_file=Path(source.origin),
        model_name=", ".join(sorted(models)),
        summary_text=final.strip() + provenance,
        generated_at=dt.datetime.now(),
    )
