import datetime as dt
import json
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
    r"too many requests|\b429\b|exceeded your|upgrade to continue|"
    # Claude Code CLI wording seen 2026-10-16: "You've hit your session limit ·
    # resets 9:30am (America/New_York)". Missing it turned a wait into three
    # failed passages on the E2d run.
    r"session limit|hit your (?:\w+ )?limit",
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
# Wall-clock form: "resets 9:30am (America/New_York)" — local time, next occurrence.
_RESET_CLOCK = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.I)
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

    clock = _RESET_CLOCK.search(text)
    if clock:
        hour = int(clock.group(1)) % 12 + (12 if clock.group(3).lower() == "pm" else 0)
        minute = int(clock.group(2) or 0)
        reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= now:
            reset += dt.timedelta(days=1)
        return reset

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
                # No tools at all, and none of the user's MCP connectors: their
                # definitions cost ~20k input tokens per call (measured 20,250 vs
                # 587 for a one-line prompt), and --disallowedTools does not
                # remove MCP tools (Gmail, Drive, ...) from an agent that is
                # reading untrusted transcript text.
                "--tools", "",
                "--strict-mcp-config",
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


# Web lookup (annotate-people): the ONLY claude -p call that gets tools, and
# only these two. Everything else stays disallowed, MCP servers are not loaded,
# and the child runs in a scratch cwd like every other CLI call.
WEB_TOOLS = "WebSearch,WebFetch"
WEB_DISALLOWED = "Skill,Read,Bash,Glob,Grep,Task,Agent,Edit,Write,NotebookEdit"


def web_lookup_argv(binary: str, alias: str, system_prompt_file: Path) -> list[str]:
    return [binary, "-p", "--model", alias,
            "--system-prompt-file", str(system_prompt_file),
            "--tools", WEB_TOOLS, "--allowedTools", WEB_TOOLS,
            "--disallowedTools", WEB_DISALLOWED,
            "--strict-mcp-config", "--output-format", "json"]


def _complete_claude_cli_web(model: str, system_prompt_file: Path, user_message: str) -> str:
    """One `claude -p` subscription call with WebSearch + WebFetch only."""
    alias = model[len(CLAUDE_CLI_PREFIX):].strip()
    binary = shutil.which(settings.claude_cli_path) or settings.claude_cli_path
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")}
    with tempfile.TemporaryDirectory(prefix="v2mdnotes-web-") as neutral_cwd:
        proc = subprocess.run(web_lookup_argv(binary, alias, system_prompt_file),
                              input=user_message, capture_output=True, text=True,
                              env=env, cwd=neutral_cwd, timeout=settings.claude_cli_web_timeout)
    out = (proc.stdout or "").strip()
    result, is_error = out, proc.returncode != 0
    try:
        data = json.loads(out)
        if isinstance(data, dict) and "result" in data:
            result = str(data.get("result") or "")
            is_error = is_error or bool(data.get("is_error"))
    except ValueError:
        pass
    if is_error:
        combined = f"{proc.stderr or ''}\n{result}"
        detail = combined.strip()[:400]
        if _EXHAUSTION.search(combined):
            raise SubscriptionExhausted(f"claude CLI subscription exhausted: {detail}",
                                        reset_at=parse_reset_time(combined))
        raise RuntimeError(f"claude CLI (web lookup) failed: {detail}")
    if not result.strip():
        raise ValueError("claude CLI web lookup returned nothing.")
    return result


def complete_web_lookup(system_prompt: str, user_message: str) -> tuple[str, str]:
    """Web lookup through the SUBSCRIPTION entries of the chain only.

    Web tools exist only in the `claude` CLI, so metered entries are never
    candidates here — whatever ALLOW_METERED_FALLBACK / --usd-cap say. Each
    entry is one call; exhaustion stops (no escalation). Returns (text, model).
    """
    errors: list[str] = []
    for model in settings.llm_models:
        if not model.startswith(CLAUDE_CLI_PREFIX):
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="v2mdnotes-web-sys-",
                                         encoding="utf-8", delete=False) as handle:
            handle.write(system_prompt)
            prompt_path = Path(handle.name)
        try:
            out = _complete_claude_cli_web(model, prompt_path, user_message)
        except SubscriptionExhausted:
            raise
        except Exception as e:  # noqa: BLE001 - try the next subscription entry
            errors.append(f"{model}: {e}")
            print(f"Web lookup: {model} failed ({e}); trying next subscription entry if any.")
            continue
        finally:
            prompt_path.unlink(missing_ok=True)
        _record_unmetered(resolve_route(model), system_prompt, user_message, out)
        return out, model
    raise RuntimeError("web lookup: no subscription (claude-cli/*) entry succeeded"
                       + (": " + "; ".join(errors) if errors else " (none configured)"))


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


# Errors raised when the provider REJECTED the request before generating
# anything: no tokens billed, so the reservation is released. Every other
# failure (timeout, empty output, connection drop, 5xx) may have been billed
# and keeps the worst-case estimate in the ledger.
_UNBILLED_ERRORS = tuple(
    getattr(litellm, name) for name in (
        "AuthenticationError", "PermissionDeniedError", "NotFoundError",
        "BadRequestError", "UnsupportedParamsError",
    ) if isinstance(getattr(litellm, name, None), type)
)


def _complete_metered(route: Route, system_prompt: str, user_message: str) -> str:
    """Cap-checked metered call: estimate, reserve, call, settle.

    Raises AnthropicApiRefused for an anthropic/* route (reserved credit)
    and BudgetExhausted BEFORE any network call when the worst case (prompt
    tokens + the full output allowance) would exceed a cap. The cap check and
    the booking of the estimate are one atomic ledger operation, so parallel
    chapter threads cannot all pass against the same total. A call that fails
    after it may have been billed stays charged at the estimate.
    """
    costs.refuse_anthropic_api(route.entry)
    costs.refuse_anthropic_api(route.litellm_model or "")
    ledger = costs.get_ledger()
    max_out = settings.metered_max_output_tokens
    prompt_tokens = costs.estimate_prompt_tokens(
        system_prompt + "\n" + user_message, route.litellm_model
    )
    estimate = costs.price_tokens(route.entry, prompt_tokens, max_out, route.litellm_model)
    rid = ledger.reserve(provider=route.ledger_provider, model=route.entry,
                         estimate_usd=estimate, total_cap=settings.metered_usd_cap,
                         provider_caps=settings.provider_usd_caps,
                         input_tokens=prompt_tokens,
                         run_cap=settings.run_usd_cap)
    try:
        content, response = _complete_response(
            route.litellm_model, route.api_key, system_prompt, user_message,
            api_base=route.api_base, max_tokens=max_out,
        )
    except _UNBILLED_ERRORS:
        try:
            ledger.release(rid)
        except Exception:  # noqa: BLE001 - leaving it charged is the safe side
            pass
        raise
    except Exception as exc:
        try:
            ledger.settle(rid, estimated=True,
                          note=f"failed after send ({type(exc).__name__}); worst-case estimate kept")
        except Exception:  # noqa: BLE001 - reservation already charges the estimate
            pass
        raise
    tin, tout, usd, estimated = costs.usage_and_cost(
        response, route.entry, route.litellm_model, fallback_usd=estimate
    )
    try:
        ledger.settle(rid, input_tokens=tin, output_tokens=tout, usd=usd, estimated=estimated)
    except Exception as exc:  # noqa: BLE001 - the call is paid for; keep its output
        print(f"Summarizer: WARNING — could not settle ${usd:.4f} for {route.entry} "
              f"in the cost ledger ({exc}); the worst-case estimate stays charged.")
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

    NOT used by any pipeline path since WP-M2: every media transcript goes
    through core/longform.py (documents use `summarize_document`). Kept as
    the reference single-call behaviour its tests pin (failover, the empty-
    transcript short-circuit); do not route new callers here.

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
            "PROVIDER_USD_CAPS / RUN_USD_CAP (--usd-cap) or point COST_LEDGER_PATH "
            "at a new ledger to continue. Details: " + "; ".join(errors),
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
_CHUNK_SYSTEM_LEGACY = """You are summarizing ONE PASSAGE of a longer book.

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
- Close every ``` fence you open.

KEY TERMS (WP-D1):
- Do not write an overview or a title: the whole-book notes get one of each.
- End with a `#### Key terms` heading and one line per term this passage
  defines: `- **Term**: definition [locator]`. Each term once.
"""

# A document that fits one passage is read in ONE call (WP-D1): the same
# locator and code rules as a passage, plus the whole-document structure the
# reduce used to impose. Built from _CHUNK_SYSTEM so the two cannot drift.
_WHOLE_SYSTEM_LEGACY = (
    _CHUNK_SYSTEM_LEGACY
    .replace("You are summarizing ONE PASSAGE of a longer book.",
             "You are writing study notes for ONE COMPLETE DOCUMENT (a book, paper,\n"
             "article or note). You are given all of it.")
    .replace("- Summarize only what this passage says. Do not infer what the rest of the book covers.",
             "- Summarize only what the document says. Add nothing that is not in it.")
    .split("KEY TERMS (WP-D1):")[0]
    + """STRUCTURE:
- First line: `# <document title>`.
- `## Overview`: one to three paragraphs — what the document is, who it is
  for, what it covers, its main argument.
- Then the main topics as `##` headings (subtopics `###`) in the document's
  own order. Each topic appears ONCE: gather everything the document says
  about it under one heading.
- Last: `## Key terms`, one line per term — `- **Term**: definition [locator]`.
  Each term once.
- Output ONLY the notes document. No preamble, no commentary, no questions or
  offers of further work. The first line of your reply is the title line.
- Be comprehensive. These notes replace the document for a reader who cannot
  open it, so do not compress away detail to be concise.
"""
)

# Prompt variant "current" (core/variants.py): the document prompts also ask
# for a People table — who the document features or discusses, their role in
# it, background ONLY as stated, relevance, locators. docnotes.consolidate
# merges every table into ONE `## People` before `## Key terms`. The
# `_LEGACY` prompts above are the variant "legacy": exactly the prompts
# before People existed.
_DOC_PEOPLE_RULES = """- Columns, in this order:
  `| person | role | affiliation & background (as stated) | relevance | locators |`
- One row per person the text features or discusses substantively: its
  authors or speakers, interviewers and interviewees, people quoted or cited,
  people whose work, claims or decisions are explained.
- person: the name as the text gives it.
- role: the person's role IN THIS TEXT, not their job title — author,
  speaker, interviewer, interviewee, panelist, quoted, cited author, person
  discussed. The one asking the questions is the interviewer; the one
  answering is the interviewee. Never swap them.
- affiliation & background (as stated): only what the text says — title,
  organisation, background, credentials. Never fill it from your own
  knowledge; if one fact is essential, add one short clause labelled
  `(per model knowledge, cutoff uncertain)`. Nothing stated: `not stated`.
  Only named: `mentioned only`.
- relevance: one or two sentences — what the person contributes, argues,
  found, built or decided here, and their stance. Attribute every claim and
  quote to the person who makes it.
- locators: the bracketed locator where the person is introduced, then up
  to three where their key points are made.
- Companies, products, models, teams and publications are not people: they
  are key terms. Private individuals (attendees, winners, customers,
  patients or students named in passing) are left out entirely.
- If no one qualifies, write the header row, the separator row and `- None`.
"""

# ... and a Sources Referenced table (WP-P2, core/cited_sources.py): the outside
# works the text cites — never people — merged into ONE section after People.
_DOC_SOURCES_RULES = """- Columns, in this order:
  `| source | type | cited by | cited for | date / edition | locators |`
- One row per outside source the text cites, quotes or relies on: outlets
  and publications, newsletters, blogs, podcasts and shows, papers, reports,
  books, posts, datasets, surveys, videos, talks, company documents.
- source: the name as the text gives it — never expanded or corrected from
  your own knowledge.
- type: outlet, newsletter, blog, podcast, show, paper, report, book, post,
  dataset, survey, video, talk, announcement, filing or other.
- cited by: who cites it in this text, by role (author, speaker, interviewee
  …). Names belong in People.
- cited for: one sentence — the claim, finding, number or quote it is cited
  for, attributed. date / edition: only what the text states, else `-`.
- locators: the bracketed locators where it is cited, up to four.
- People are not sources: a person goes in People. A person's own podcast,
  newsletter, paper or book IS a source (and the person stays in People).
- No outside knowledge, except one short clause labelled `(per model
  knowledge)` when it is essential to identify the source.
- If the text cites no outside source, write the header row, the separator
  row and `- None`.
"""

_CHUNK_SYSTEM = _CHUNK_SYSTEM_LEGACY.replace(
    "KEY TERMS (WP-D1):",
    "PEOPLE:\n- Before the key terms, add a `#### People` heading and a table of the\n"
    "  people in this passage.\n" + _DOC_PEOPLE_RULES
    + "\nSOURCES REFERENCED:\n- After People, add a `#### Sources Referenced` heading and a\n"
    "  table of the outside sources this passage cites.\n" + _DOC_SOURCES_RULES
    + "\nKEY TERMS (WP-D1):")
_WHOLE_SYSTEM = _WHOLE_SYSTEM_LEGACY.replace(
    "- Last: `## Key terms`",
    "- Then `## People`, immediately before Key terms: a table of the people\n"
    "  in the document.\n" + "\n".join("  " + ln if ln.strip() else ln
                                       for ln in _DOC_PEOPLE_RULES.rstrip().split("\n"))
    + "\n- Then `## Sources Referenced`, after People: a table of the outside\n"
    "  sources the document cites.\n" + "\n".join("  " + ln if ln.strip() else ln
                                                 for ln in _DOC_SOURCES_RULES.rstrip().split("\n"))
    + "\n- Last: `## Key terms`")
assert _CHUNK_SYSTEM != _CHUNK_SYSTEM_LEGACY and _WHOLE_SYSTEM != _WHOLE_SYSTEM_LEGACY


def _chunk_system() -> str:
    from video2mdnotes.core import variants
    return _CHUNK_SYSTEM if variants.has_people() else _CHUNK_SYSTEM_LEGACY


def _whole_system() -> str:
    from video2mdnotes.core import variants
    return _WHOLE_SYSTEM if variants.has_people() else _WHOLE_SYSTEM_LEGACY


# Passages combined per reduce call. Six keeps each call small enough to finish
# inside the CLI timeout; a 16-passage reduce timed out at 900s and lost the
# whole book's work.
_REDUCE_BATCH = 6

_REDUCE_SYSTEM = """You are assembling per-passage notes for one book into a single set of notes.

Rules:
- Work only from the notes given. Add nothing that is not in them.
- Keep the locators. A reader must be able to find every claim in the book.
- Merge duplicates; preserve disagreements rather than resolving them silently.
- Structure: the main topics in the book's own order, as `##` headings
  (subtopics `###`). Each topic appears ONCE — gather what several passages
  say about it under one heading.
- Do NOT write a title, an overview or a key-terms section. Those are built
  once for the whole book, separately; a per-batch overview is what gave one
  document four of them.
- CODE BLOCKS PASS THROUGH VERBATIM. Copy every ``` fenced block from the notes
  into your output unchanged. Never summarize a code block into a sentence, and
  never drop one because a nearby passage covers the same topic — the reduce is
  where code silently disappears. Close every fence you open.
- Output ONLY the notes document. No preamble, no commentary on the input, no
  checklist of what you did, and no questions or offers of further work. The
  first line of your reply is the first line of the document.
- Be comprehensive. These notes replace the book for a reader who cannot open
  it, so do not compress away detail to be concise.
"""

# One Overview for the whole document, written once over the assembled topic
# notes (or their outline when those are long). Small input, small output.
_OVERVIEW_SYSTEM = """You are writing the Overview section of study notes for one document.

Rules:
- Work only from the notes given. Add nothing that is not in them.
- One to three paragraphs: what the document is, who it is for, what it
  covers in order, and its main argument.
- Keep a bracketed locator on any specific claim, copied from the notes.
- Output ONLY the paragraphs: no heading, no preamble, no questions.
"""

# Notes this large go to the overview call as an outline instead.
_OVERVIEW_FULL_WORDS = 12_000


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


def _drop_title_line(text: str) -> str:
    """A batch's own `# Title` line: the document has one title, at the top."""
    lines = text.strip().split("\n")
    if lines and re.match(r"^#[ \t]+\S", lines[0]):
        lines = lines[1:]
    return "\n".join(lines).strip()


def _write_overview(title: str, topics: str) -> tuple[str, str | None, str | None]:
    """(overview text, model or None, failure reason or None).

    Falls back to a deterministic list of the topic headings — never to a
    second reduce, and never to nothing.
    """
    from video2mdnotes.core import docnotes

    body = topics if len(topics.split()) <= _OVERVIEW_FULL_WORDS else docnotes.outline(topics)
    reason = None
    try:
        output, model = complete_with_failover(
            _OVERVIEW_SYSTEM, f"Document: {title}\n\n{body}")
        output = _strip_chat_preamble(output)
        output = "\n".join(line for line in output.split("\n")
                           if not re.match(r"^#{1,6}[ \t]+overview\b", line, re.I)).strip()
        if len(output.split()) >= 25 and not _CHAT_OFFER.search(output):
            return output, model, None
        reason = f"overview call returned {len(output.split())} unusable words"
    except (SubscriptionExhausted, BudgetExhausted):
        raise
    except Exception as exc:  # noqa: BLE001 - the topics are the expensive part
        reason = f"overview call failed: {str(exc)[:160]}"
    heads = docnotes.topic_headings(topics)
    listed = "; ".join(_BRACKETS.sub("", h).strip() for h in heads) or "see the topics below"
    return f"These notes cover, in order: {listed}.", None, reason


_BRACKETS = re.compile(r"\s*\[[^\]]*\]")


def _finish_notes(text: str, label: str, extra_terms=(), extra_people=(),
                  extra_sources=()) -> tuple[str, dict]:
    """One Overview, one merged People and one merged Sources Referenced
    (variant "current"), one merged Key terms, balanced code fences.
    Deterministic."""
    from video2mdnotes.core import docnotes, fences, variants

    done = docnotes.consolidate(text, extra_terms, people=variants.has_people(),
                                extra_people=extra_people,
                                sources=variants.has_sources(),
                                extra_sources=extra_sources)
    fixed, repairs = fences.repair_fences(done.text, label=label)
    info = {
        "overviews": max(done.overviews_found - done.overviews_dropped, 0),
        "overviews_dropped": done.overviews_dropped,
        "key_terms_in": done.terms_in, "key_terms_out": done.terms_out,
        "key_term_lists_merged": done.term_sections,
        "people_rows_in": done.people_in, "people_out": done.people_out,
        "sources_rows_in": done.sources_in, "sources_out": done.sources_out,
        "fence_repairs": repairs,
    }
    return fixed, info


def _variant() -> str:
    from video2mdnotes.core import variants
    return variants.active()


def summarize_document(source, max_words: int = None, progress=None) -> SummaryResult:
    """Summarize a `SourceText` (book, paper, article, note).

    WP-D1 shape:

    * **Fits the budget → ONE call** over the whole document, every section
      locator intact, no reduce. T8 had cut an 8,956-word note into 22
      passages and concatenated four reduce batches: four `## Overview`s,
      three `## Key terms`, topics split at seams.
    * **Longer → packed passages** (`chunking.pack_sections`): whole adjacent
      chapters up to the budget, seams between chapters; a chapter is split
      only when it alone is over budget. Each passage ends with its own key
      terms, which are split off before the reduce; the reduce (one round of
      batches, concatenated) writes topics only; one overview call writes the
      Overview; the key terms are merged deterministically into ONE list.

    Unchanged load-bearing properties:

    * **Locators survive.** Every section carries its own locator into the
      prompt (`Chunk.to_prompt_text`).
    * **Exhaustion stops the whole document, not one chunk.**
    * **A reduce failure keeps the passage notes** (`_fallback`), and the
      `_reduce_collapsed` guard rejects chat or fragments.

    The raw text is deliberately NOT appended the way a transcript is: book text
    is copyrighted, and writing a whole book into an artifact that may land in a
    vault or a repo is not something to do by default.
    """
    from video2mdnotes.core import docnotes
    from video2mdnotes.core.chunking import pack_sections

    chunks = pack_sections(source.sections, **(
        {"max_words": max_words} if max_words else {}))
    if not chunks:
        return SummaryResult(
            source_file=Path(source.origin), model_name="none",
            summary_text=EMPTY_TRANSCRIPT_PLACEHOLDER,
            generated_at=dt.datetime.now(),
        )

    models: set[str] = set()
    calls = 0
    label = getattr(source, "source_id", None) or source.title
    info: dict = {}

    if len(chunks) == 1:
        chunk = chunks[0]
        if progress:
            progress(chunk.index, chunk.total, chunk.locator)
        header = (f"Document: {source.title}\n"
                  f"The complete document — locator span [{chunk.locator}]\n\n")
        output, model = complete_with_failover(_whole_system(), header + chunk.to_prompt_text())
        models.add(model)
        calls += 1
        output = _strip_chat_preamble(output)
        if not output.strip() or output.strip().upper() == "SKIP":
            return SummaryResult(
                source_file=Path(source.origin), model_name=model,
                summary_text=EMPTY_TRANSCRIPT_PLACEHOLDER,
                generated_at=dt.datetime.now(),
            )
        if len(output.split()) < 300 and source.word_count > 2_000:
            raise RuntimeError(
                f"the document call returned {len(output.split())} words for a "
                f"{source.word_count:,}-word document — chat or truncation, not notes")
        final, info = _finish_notes(output, label)
    else:
        from video2mdnotes.core import people as people_mod
        from video2mdnotes.core import cited_sources as sources_mod
        notes: list[str] = []
        passage_sources: list = []
        passage_terms: list[str] = []
        passage_people: list = []
        for chunk in chunks:
            if progress:
                progress(chunk.index, chunk.total, chunk.locator)
            header = (
                f"Book: {source.title}\n"
                f"Passage {chunk.index} of {chunk.total} — locator [{chunk.locator}]\n\n"
            )
            output, model = complete_with_failover(
                _chunk_system(), header + chunk.to_prompt_text()
            )
            models.add(model)
            calls += 1
            if output.strip().upper() != "SKIP":
                body, terms, _ = docnotes.split_terms(output.strip())
                passage_terms.extend(terms)
                body, rows, _ = people_mod.split_people(body)
                passage_people.extend(rows)
                body, srows, _ = sources_mod.split_sources(body)
                passage_sources.extend(srows)
                notes.append(f"### [{chunk.locator}] {' > '.join(chunk.heading_path)}\n"
                             f"{body.strip()}")

        if not notes:
            return SummaryResult(
                source_file=Path(source.origin),
                model_name=", ".join(sorted(models)) or "none",
                summary_text=EMPTY_TRANSCRIPT_PLACEHOLDER,
                generated_at=dt.datetime.now(),
            )

        # The per-passage notes are the expensive part — one model call each,
        # and for a book that is most of the run. Everything below is a
        # *reduce* over them, and a reduce failure must never discard them.
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
        # everything — compounds loss at every round, and the second round
        # hands the model text that is already assembled notes, which it reads
        # as finished work ("This is excellent work ✓ … Do you want me to merge
        # Chapters 1–5?" — 177 words of chat in place of a book).
        #
        # Concatenation is safe now that batches write TOPICS ONLY: the one
        # Overview and the one Key terms list are built for the whole document
        # below, so seams no longer repeat them.
        try:
            parts = []
            for i in range(0, len(notes), _REDUCE_BATCH):
                batch = notes[i:i + _REDUCE_BATCH]
                scope = ("" if len(notes) <= _REDUCE_BATCH else
                         f"You are given passages {i + 1}-{i + len(batch)} of "
                         f"{len(notes)}; another pass covers the rest. Do NOT title "
                         f"the output after the passage numbers - that is internal "
                         f"batching a reader should never see. Use the topics.\n")
                output, model = complete_with_failover(
                    _REDUCE_SYSTEM, f"Book: {source.title}\n{scope}\n" + "\n\n".join(batch))
                models.add(model)
                calls += 1
                parts.append(_drop_title_line(_strip_chat_preamble(output)))
            topics = "\n\n".join(parts)
            topics, reduce_terms, _ = docnotes.split_terms(topics)
            if _reduce_collapsed(topics, passage_notes):
                final, info = _finish_notes(_fallback(
                    f"the reduce returned {len(topics.split())} words from "
                    f"{sum(len(n.split()) for n in passage_notes)} words of passage "
                    f"notes — it chatted or truncated instead of assembling"
                ), label, passage_terms, passage_people, passage_sources)
            else:
                overview, model, why = _write_overview(source.title, topics)
                if model:
                    models.add(model)
                    calls += 1
                elif why:
                    calls += 1
                assembled = (f"# {source.title}\n\n## Overview\n\n{overview}\n\n"
                             f"{topics.strip()}\n")
                final, info = _finish_notes(assembled, label, passage_terms + reduce_terms,
                                            passage_people, passage_sources)
                if why:
                    info["overview_fallback"] = why
        except (SubscriptionExhausted, BudgetExhausted):
            raise
        except Exception as exc:  # noqa: BLE001 - keep the work that succeeded
            final, info = _finish_notes(_fallback(str(exc)[:200]), label, passage_terms,
                                        passage_people, passage_sources)

    repairs = info.get("fence_repairs") or []
    provenance = (
        f"\n\n---\nsource: {source.origin}\n"
        f"provenance: {source.provenance}\n"
        f"structure_is_inferred: {source.structure_is_inferred}\n"
        f"passages: {len(chunks)}  words: {source.word_count:,}  model_calls: {calls}\n"
        f"models: {', '.join(sorted(models))}\n"
        f"prompt_variant: {_variant()}\n"
        + (f"code_fence_repairs: {len(repairs)}\n" if repairs else "")
    )
    return SummaryResult(
        source_file=Path(source.origin),
        model_name=", ".join(sorted(models)),
        summary_text=final.strip() + provenance,
        generated_at=dt.datetime.now(),
    )
