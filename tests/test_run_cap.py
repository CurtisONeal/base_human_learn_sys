"""Per-run spend cap (RUN_USD_CAP / --usd-cap / API usd_cap), on top of the
cumulative METERED_USD_CAP. litellm and the claude CLI are mocked; the autouse
fixture in conftest.py gives each test its own ledger and restores settings.
"""
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from video2mdnotes.config import Settings, settings
from video2mdnotes.core import costs, visuals
from video2mdnotes.core.costs import BudgetExhausted, CostLedger
from video2mdnotes.core.summarizer import complete_with_failover


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-openai")
    monkeypatch.setattr(settings, "on_exhaustion", "wait")
    monkeypatch.setattr(settings, "allow_local_models", False)
    monkeypatch.setattr(settings, "provider_usd_caps", {})
    monkeypatch.setattr(settings, "metered_usd_cap", 10.0)
    monkeypatch.setattr(settings, "run_id", "run-under-test")
    return lambda models: monkeypatch.setattr(settings, "llm_models", models)


def _ok(prompt_tokens=1000, completion_tokens=16000):
    resp = MagicMock()
    resp.choices[0].message.content = "# Notes"
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    return resp


def _spend(usd, run_id):
    CostLedger(settings.cost_ledger_path).record(
        provider="openai", model="openai/x", kind=costs.METERED,
        input_tokens=0, output_tokens=0, usd=usd, run_id=run_id)


def test_run_cap_blocks_second_call_while_cumulative_has_room(chain, monkeypatch):
    chain(["openai/gpt-4o"])
    # $5 spent by an EARLIER run: counts against the cumulative $10, not this run.
    _spend(5.0, run_id="an-earlier-run")
    monkeypatch.setattr(settings, "run_usd_cap", 0.30)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        comp.return_value = _ok()                  # ~$0.1625 at gpt-4o prices
        out, model = complete_with_failover("sys", "user")
        assert model == "openai/gpt-4o"
        with pytest.raises(BudgetExhausted) as exc:
            complete_with_failover("sys", "user")
        assert comp.call_count == 1                # refused BEFORE the network
    assert "per-run cap $0.30" in str(exc.value)
    led = costs.get_ledger()
    assert led.spent(run_id="run-under-test") == pytest.approx(0.1625, rel=0.01)
    assert led.spent() == pytest.approx(5.1625, rel=0.01)   # cumulative still < $10
    # Without the per-run cap the same second call is allowed.
    monkeypatch.setattr(settings, "run_usd_cap", None)
    with patch("video2mdnotes.core.summarizer.litellm.completion", return_value=_ok()):
        complete_with_failover("sys", "user")


def test_open_reservations_count_against_the_run_cap(tmp_path):
    led = CostLedger(tmp_path / "l.json")
    led.reserve(provider="openai", model="openai/gpt-4o", estimate_usd=0.2,
                total_cap=10, provider_caps={}, run_id="r1", run_cap=0.3)
    with pytest.raises(BudgetExhausted, match="per-run cap"):
        led.reserve(provider="openai", model="openai/gpt-4o", estimate_usd=0.2,
                    total_cap=10, provider_caps={}, run_id="r1", run_cap=0.3)
    # A different run has its own per-run budget.
    led.reserve(provider="openai", model="openai/gpt-4o", estimate_usd=0.2,
                total_cap=10, provider_caps={}, run_id="r2", run_cap=0.3)


def test_cumulative_cap_still_binds_under_a_generous_run_cap(chain, monkeypatch):
    chain(["openai/gpt-4o"])
    _spend(9.95, run_id="an-earlier-run")
    monkeypatch.setattr(settings, "run_usd_cap", 100.0)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(BudgetExhausted, match="metered cap"):
            complete_with_failover("sys", "user")
        comp.assert_not_called()


def test_run_cap_zero_forbids_any_metered_call(chain, monkeypatch):
    chain(["openai/gpt-4o"])
    monkeypatch.setattr(settings, "run_usd_cap", 0.0)
    # Even a call that would be priced at $0 is refused: 0 means "none".
    monkeypatch.setattr(costs, "price_tokens", lambda *a, **k: 0.0)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(BudgetExhausted) as exc:
            complete_with_failover("sys", "user")
        comp.assert_not_called()
    assert "per-run cap is $0" in str(exc.value)
    assert costs.get_ledger().entries() == []
    assert visuals.vlm_allowed() is False       # the un-ledgered vision tier is off too


def test_subscription_unaffected_by_run_cap_zero(chain, monkeypatch):
    chain(["claude-cli/opus", "openai/gpt-4o"])
    monkeypatch.setattr(settings, "run_usd_cap", 0.0)
    cli = MagicMock(returncode=0, stdout="# Subscription notes", stderr="")
    with patch("video2mdnotes.core.summarizer.subprocess.run", return_value=cli), \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        out, model = complete_with_failover("sys", "user")
        comp.assert_not_called()
    assert (out, model) == ("# Subscription notes", "claude-cli/opus")
    [entry] = costs.get_ledger().entries()
    assert entry["kind"] == costs.SUBSCRIPTION and entry["usd"] == 0.0


def test_run_cap_parses_from_environment(monkeypatch):
    monkeypatch.delenv("RUN_USD_CAP", raising=False)
    assert Settings(_env_file=None).run_usd_cap is None
    monkeypatch.setenv("RUN_USD_CAP", "")
    assert Settings(_env_file=None).run_usd_cap is None     # blank = unset
    monkeypatch.setenv("RUN_USD_CAP", "2.5")
    assert Settings(_env_file=None).run_usd_cap == 2.5
    monkeypatch.setenv("RUN_USD_CAP", "0")
    assert Settings(_env_file=None).run_usd_cap == 0.0


@pytest.mark.parametrize("command", ["process", "media", "book", "longform"])
def test_usd_cap_flag_is_offered(command):
    from video2mdnotes.main import app
    result = CliRunner().invoke(app, [command, "--help"])
    assert result.exit_code == 0 and "--usd-cap" in result.output


def test_usd_cap_flag_overrides_env(monkeypatch, tmp_path):
    """RUN_USD_CAP=5 in the environment; --usd-cap 0 wins for this invocation."""
    from video2mdnotes.main import app

    monkeypatch.setenv("RUN_USD_CAP", "5")
    monkeypatch.setattr(settings, "run_usd_cap", Settings(_env_file=None).run_usd_cap)
    assert settings.run_usd_cap == 5.0
    doc = tmp_path / "note.md"
    doc.write_text("# T\n\nwords here\n")
    result = CliRunner().invoke(app, ["book", str(doc), "--extract-only", "--usd-cap", "0"])
    assert settings.run_usd_cap == 0.0, result.output
    # No flag: the environment value stands.
    monkeypatch.setattr(settings, "run_usd_cap", 5.0)
    CliRunner().invoke(app, ["book", str(doc), "--extract-only"])
    assert settings.run_usd_cap == 5.0


def test_negative_usd_cap_is_rejected(tmp_path):
    from video2mdnotes.main import app
    doc = tmp_path / "note.md"
    doc.write_text("# T\n\nwords\n")
    result = CliRunner().invoke(app, ["book", str(doc), "--extract-only", "--usd-cap", "-1"])
    assert result.exit_code == 2 and "--usd-cap must be >= 0" in result.output
