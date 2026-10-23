import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import datetime as dt

from video2mdnotes.core.summarizer import (
    generate_summary,
    SubscriptionExhausted,
    parse_reset_time,
    SummaryResult,
    EMPTY_TRANSCRIPT_PLACEHOLDER,
)
from video2mdnotes.core.transcriber import TranscriptResult, Segment
from video2mdnotes.config import settings

# --- Fixtures ---

@pytest.fixture
def mock_transcript_result(tmp_path):
    """Creates a dummy TranscriptResult for testing."""
    return TranscriptResult(
        source_file=tmp_path / "test.wav",
        language="en",
        segments=[Segment(start=0.0, end=1.5, text="This is a test transcript.")],
        full_text="This is a test transcript.",
        markdown_content="# Test Transcript\n\nThis is a test transcript.",
        model_name="tiny",
        generated_at=dt.datetime.now()
    )

@pytest.fixture
def mock_prompt_file(tmp_path):
    """Creates a temporary prompt file and updates settings to point to it."""
    p = tmp_path / "prompt.txt"
    p.write_text("You are a summarizer.", encoding="utf-8")
    
    original_prompt_file = settings.prompt_file
    settings.prompt_file = p
    yield p
    settings.prompt_file = original_prompt_file

# --- Unit Tests (Mocked) ---

@pytest.fixture
def pinned_chain():
    """Pin the failover chain per-test.

    Hermetic tests MUST pin this. The default chain leads with a `claude-cli/`
    entry, which is a subprocess — patching `litellm.completion` does not
    intercept it, so an unpinned test will spawn a real `claude` CLI and make a
    live subscription call.
    """
    saved = (
        settings.llm_models,
        settings.openai_api_key,
        settings.anthropic_api_key,
        settings.allow_local_models,
        settings.on_exhaustion,
    )
    yield lambda models: setattr(settings, "llm_models", models)
    (
        settings.llm_models,
        settings.openai_api_key,
        settings.anthropic_api_key,
        settings.allow_local_models,
        settings.on_exhaustion,
    ) = saved


def test_generate_summary_mocked(mock_transcript_result, mock_prompt_file, pinned_chain):
    """Test the summarization logic with a mocked LLM call (single metered entry)."""

    pinned_chain(["openai/gpt-4o"])
    settings.openai_api_key = "test-key"  # hermetic: don't depend on .env

    with patch("video2mdnotes.core.summarizer.litellm.completion") as mock_completion:
        mock_response = MagicMock()
        mock_response.choices[0].message.content = "# Summary\nThis is a summary."
        mock_completion.return_value = mock_response

        result = generate_summary(mock_transcript_result)

        assert isinstance(result, SummaryResult)
        assert "# Summary\nThis is a summary." in result.summary_text
        assert "## Transcript\nThis is a test transcript." in result.summary_text
        assert result.model_name == "openai/gpt-4o"

        mock_completion.assert_called_once()
        messages = mock_completion.call_args.kwargs["messages"]

        # System prompt comes from the fixture file; user message is the transcript.
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are a summarizer."
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == mock_transcript_result.markdown_content


def test_chain_falls_back_to_next_entry(mock_transcript_result, mock_prompt_file, pinned_chain):
    """A failing entry must fall through to the next one in the chain."""
    pinned_chain(["openai/gpt-4o", "anthropic/claude-haiku-4-5"])
    settings.openai_api_key = "openai-key"
    settings.anthropic_api_key = "anthropic-key"
    settings.allow_anthropic_api = True        # deliberate opt-in (F9 (g))

    with patch("video2mdnotes.core.summarizer.litellm.completion") as mock_completion:
        ok = MagicMock()
        ok.choices[0].message.content = "# Summary\nFrom Anthropic."
        mock_completion.side_effect = [RuntimeError("openai down"), ok]

        result = generate_summary(mock_transcript_result)

        assert "From Anthropic." in result.summary_text
        assert result.model_name == "anthropic/claude-haiku-4-5"
        assert mock_completion.call_count == 2
        assert mock_completion.call_args_list[1].kwargs["model"] == "anthropic/claude-haiku-4-5"
        # Each entry gets its own provider's key, not a shared global.
        assert mock_completion.call_args_list[0].kwargs["api_key"] == "openai-key"
        assert mock_completion.call_args_list[1].kwargs["api_key"] == "anthropic-key"


def test_claude_cli_backend_disables_tools_and_strips_keys(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """The subscription backend's two safety guards must actually reach the CLI.

    Tools disabled + neutral cwd keeps an agent away from untrusted transcript
    text; stripped keys keep billing on the subscription instead of metered API.
    """
    pinned_chain(["claude-cli/opus"])

    with patch("video2mdnotes.core.summarizer.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="# Summary\nFrom the CLI.", stderr=""
        )
        with patch.dict(
            "os.environ",
            {"ANTHROPIC_API_KEY": "leak-me", "OPENAI_API_KEY": "leak-me-too"},
        ):
            result = generate_summary(mock_transcript_result)

        assert "From the CLI." in result.summary_text
        assert result.model_name == "claude-cli/opus"

        argv = mock_run.call_args.args[0]
        assert "--model" in argv and argv[argv.index("--model") + 1] == "opus"
        assert "--disallowedTools" in argv
        assert "Skill" in argv[argv.index("--disallowedTools") + 1]
        assert "--system-prompt-file" in argv
        # No tool or MCP definitions reach the model (cost + injection surface).
        assert argv[argv.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in argv

        kwargs = mock_run.call_args.kwargs
        # Transcript goes in on stdin, not argv (a full transcript blows ARG_MAX).
        assert kwargs["input"] == mock_transcript_result.markdown_content
        # Keys stripped so the CLI uses subscription auth, not metered billing.
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
        assert "OPENAI_API_KEY" not in kwargs["env"]
        # Neutral cwd, so no repo CLAUDE.md is pulled into the job.
        assert kwargs["cwd"] is not None


def test_claude_cli_nonzero_exit_falls_through(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """A CLI failure (e.g. rate limit) must fall through to the metered entry."""
    pinned_chain(["claude-cli/opus", "openai/gpt-4o"])
    settings.openai_api_key = "openai-key"

    with patch("video2mdnotes.core.summarizer.subprocess.run") as mock_run, \
         patch("video2mdnotes.core.summarizer.litellm.completion") as mock_completion:
        mock_run.return_value = MagicMock(
            returncode=1, stdout="", stderr="claude: command crashed"
        )
        ok = MagicMock()
        ok.choices[0].message.content = "# Summary\nFrom OpenAI."
        mock_completion.return_value = ok

        result = generate_summary(mock_transcript_result)

        assert "From OpenAI." in result.summary_text
        assert result.model_name == "openai/gpt-4o"


def test_local_model_blocked_unless_explicitly_allowed(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """Local models need no key, so only the gate stops a silent downgrade."""
    pinned_chain(["ollama/llama3.1"])
    settings.allow_local_models = False

    with patch("video2mdnotes.core.summarizer.litellm.completion") as mock_completion:
        with pytest.raises(RuntimeError, match="ALLOW_LOCAL_MODELS"):
            generate_summary(mock_transcript_result)
        mock_completion.assert_not_called()


def test_local_model_runs_without_api_key_when_allowed(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """With the gate open, a local entry runs and is not asked for a key."""
    pinned_chain(["ollama/llama3.1"])
    settings.allow_local_models = True

    with patch("video2mdnotes.core.summarizer.litellm.completion") as mock_completion:
        ok = MagicMock()
        ok.choices[0].message.content = "# Summary\nFrom Ollama."
        mock_completion.return_value = ok

        result = generate_summary(mock_transcript_result)

        assert "From Ollama." in result.summary_text
        assert result.model_name == "ollama/llama3.1"
        assert mock_completion.call_args.kwargs["api_key"] is None


def test_generate_summary_empty_transcript_short_circuits(mock_transcript_result, mock_prompt_file):
    """An empty (zero-segment) transcript must return a placeholder without calling the LLM."""
    empty_transcript = mock_transcript_result.model_copy(
        update={"segments": [], "full_text": "", "markdown_content": ""}
    )

    with patch("video2mdnotes.core.summarizer.litellm.completion") as mock_completion:
        result = generate_summary(empty_transcript)

        mock_completion.assert_not_called()
        assert isinstance(result, SummaryResult)
        assert result.summary_text == EMPTY_TRANSCRIPT_PLACEHOLDER
        assert "## Transcript" not in result.summary_text


def test_generate_summary_missing_prompt_file(mock_transcript_result):
    """Test that FileNotFoundError is raised if prompt file is missing."""
    # Point settings to a non-existent file
    original_prompt_file = settings.prompt_file
    settings.prompt_file = Path("/non/existent/file.txt")
    
    try:
        with pytest.raises(FileNotFoundError):
            generate_summary(mock_transcript_result)
    finally:
        settings.prompt_file = original_prompt_file

# --- Integration Tests (Real API Call) ---

@pytest.mark.integration
def test_generate_summary_integration():
    """
    Integration test that actually calls the LLM API.
    Requires OPENAI_API_KEY (or configured provider key) in .env.
    """
    import litellm
    
    # Skip if no API key is present
    if not settings.openai_api_key and not settings.anthropic_api_key:
        pytest.skip("No API key found in settings. Skipping integration test.")

    try:
        # Simple call to verify connectivity
        response = litellm.completion(
            model=settings.llm_model,
            messages=[{"role": "user", "content": "Say 'Integration Test Passed'"}],
            max_tokens=10
        )
        content = response.choices[0].message.content
        assert content is not None
        # We don't strictly check the text because LLMs vary, but it shouldn't be empty
        assert len(content) > 0
        
    except Exception as e:
        pytest.fail(f"LLM Integration test failed: {e}")


# --- Subscription exhaustion (research runs must not spend money unattended) ---

def _exhausted_proc():
    return MagicMock(returncode=1, stdout="",
                     stderr="Usage limit reached. Try again in 3 hours 20 minutes.")


def test_exhaustion_does_not_escalate_to_metered_by_default(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """The whole point: nobody is there to consent, so nothing paid may run."""
    pinned_chain(["claude-cli/opus", "claude-cli/sonnet", "openai/gpt-4o"])
    settings.openai_api_key = "sk-test"
    settings.on_exhaustion = "wait"   # main.py turns this into a wait+retry

    with patch("video2mdnotes.core.summarizer.subprocess.run") as run, \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        run.return_value = _exhausted_proc()
        with pytest.raises(SubscriptionExhausted):
            generate_summary(mock_transcript_result)
        comp.assert_not_called()   # no paid call was made


def test_exhaustion_escalates_to_metered_only_when_asked(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    pinned_chain(["claude-cli/opus", "openai/gpt-4o"])
    settings.openai_api_key = "sk-test"
    settings.on_exhaustion = "metered"

    with patch("video2mdnotes.core.summarizer.subprocess.run") as run, \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        run.return_value = _exhausted_proc()
        ok = MagicMock()
        ok.choices[0].message.content = "# Summary"
        comp.return_value = ok

        result = generate_summary(mock_transcript_result)

    assert result.model_name == "openai/gpt-4o"


def test_exhaustion_local_policy_skips_paid_backends(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """'local' must not be a backdoor to metered spending."""
    pinned_chain(["claude-cli/opus", "openai/gpt-4o", "ollama/llama3.1"])
    settings.openai_api_key = "sk-test"
    settings.allow_local_models = True
    settings.on_exhaustion = "local"

    with patch("video2mdnotes.core.summarizer.subprocess.run") as run, \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        run.return_value = _exhausted_proc()
        ok = MagicMock()
        ok.choices[0].message.content = "# Summary"
        comp.return_value = ok

        result = generate_summary(mock_transcript_result)

    assert result.model_name == "ollama/llama3.1"
    # Exactly one litellm call, and it was the local one — never openai.
    assert comp.call_count == 1
    assert comp.call_args.kwargs["model"] == "ollama/llama3.1"


def test_ordinary_failure_still_falls_through(
    mock_transcript_result, mock_prompt_file, pinned_chain
):
    """A crash is not exhaustion; normal failover must be unaffected."""
    pinned_chain(["claude-cli/opus", "openai/gpt-4o"])
    settings.openai_api_key = "sk-test"
    settings.on_exhaustion = "wait"

    with patch("video2mdnotes.core.summarizer.subprocess.run") as run, \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        run.return_value = MagicMock(returncode=1, stdout="", stderr="segfault")
        ok = MagicMock()
        ok.choices[0].message.content = "# Summary"
        comp.return_value = ok

        result = generate_summary(mock_transcript_result)

    assert result.model_name == "openai/gpt-4o"


@pytest.mark.parametrize("text,hours", [
    ("Usage limit reached. Try again in 3 hours 20 minutes.", 3),
    ("rate limit exceeded, retry after 45 minutes", 0),
])
def test_parse_reset_time_relative(text, hours):
    now = dt.datetime(2026, 8, 16, 10, 0, 0)
    got = parse_reset_time(text, now=now)
    assert got is not None and got > now
    assert (got - now).total_seconds() >= hours * 3600


def test_parse_reset_time_absent_returns_none():
    assert parse_reset_time("something else entirely") is None


def test_session_limit_wording_is_exhaustion_with_clock_reset():
    """E2d (2026-10-16): the CLI said "You've hit your session limit · resets
    9:30am (America/New_York)"; it was not recognised, so passages failed
    instead of waiting for the reset."""
    import datetime as dt
    from video2mdnotes.core.summarizer import _EXHAUSTION

    msg = "claude CLI exited 1: You've hit your session limit · resets 9:30am (America/New_York)"
    assert _EXHAUSTION.search(msg)
    now = dt.datetime(2026, 10, 16, 5, 12)
    assert parse_reset_time(msg, now=now) == dt.datetime(2026, 10, 16, 9, 30)
    late = dt.datetime(2026, 10, 16, 10, 0)
    assert parse_reset_time(msg, now=late) == dt.datetime(2026, 10, 17, 9, 30)
    assert parse_reset_time("resets 4:20am", now=now) == dt.datetime(2026, 10, 17, 4, 20)
