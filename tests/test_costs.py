"""WP-8: cost ledger, spend caps, and provider routing (no network).

litellm.completion and the claude CLI subprocess are mocked throughout; the
autouse fixture in conftest.py gives each test its own ledger file.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from video2mdnotes.config import Settings, settings
from video2mdnotes.core import costs, summarizer
from video2mdnotes.core.costs import BudgetExhausted, CostLedger
from video2mdnotes.core.summarizer import (
    SubscriptionExhausted,
    complete_with_failover,
    resolve_route,
)


@pytest.fixture
def chain(monkeypatch):
    """Pin the chain and keys; restore afterwards."""
    monkeypatch.setattr(settings, "openai_api_key", "sk-test-openai")
    monkeypatch.setattr(settings, "nous_api_key", "nous-test-key")
    monkeypatch.setattr(settings, "on_exhaustion", "wait")
    monkeypatch.setattr(settings, "allow_local_models", False)
    monkeypatch.setattr(settings, "provider_usd_caps", {})
    monkeypatch.setattr(settings, "metered_usd_cap", 10.0)
    return lambda models: monkeypatch.setattr(settings, "llm_models", models)


def _ok(text="# Notes", prompt_tokens=1000, completion_tokens=1000):
    resp = MagicMock()
    resp.choices[0].message.content = text
    resp.usage.prompt_tokens = prompt_tokens
    resp.usage.completion_tokens = completion_tokens
    return resp


def _cli_ok():
    return MagicMock(returncode=0, stdout="# Subscription notes", stderr="")


def _cli_exhausted():
    return MagicMock(returncode=1, stdout="",
                     stderr="Usage limit reached. Try again in 3 hours.")


def _spend(provider, usd, path=None):
    CostLedger(path or settings.cost_ledger_path).record(
        provider=provider, model=f"{provider}/x", kind=costs.METERED,
        input_tokens=0, output_tokens=0, usd=usd,
    )


# --- Ledger arithmetic and persistence ---------------------------------------

def test_ledger_totals_and_persistence(tmp_path):
    path = tmp_path / "ledger.json"
    led = CostLedger(path)
    led.record(provider="openai", model="openai/gpt-4o", kind=costs.METERED,
               input_tokens=100, output_tokens=50, usd=0.5, run_id="r1")
    led.record(provider="nous", model="nous/Hermes-4-405B", kind=costs.METERED,
               input_tokens=100, output_tokens=50, usd=0.25, run_id="r1")
    led.record(provider="claude-cli", model="claude-cli/opus", kind=costs.SUBSCRIPTION,
               input_tokens=100, output_tokens=50, usd=99.0, run_id="r2")

    # A fresh object over the same file sees everything (persistence).
    again = CostLedger(path)
    assert again.spent() == pytest.approx(0.75)
    assert again.spent("openai") == pytest.approx(0.5)
    assert again.spent("nous") == pytest.approx(0.25)
    assert again.calls() == 3
    assert again.calls(kind=costs.SUBSCRIPTION) == 1

    data = json.loads(path.read_text())
    sub = [e for e in data["entries"] if e["kind"] == costs.SUBSCRIPTION][0]
    assert sub["usd"] == 0.0          # subscription is never charged, whatever is passed
    assert {e["run_id"] for e in data["entries"]} == {"r1", "r2"}
    for key in ("timestamp", "provider", "model", "input_tokens", "output_tokens"):
        assert key in data["entries"][0]


def test_check_blocks_at_total_and_provider_caps(tmp_path):
    led = CostLedger(tmp_path / "l.json")
    _spend("openai", 4.0, led.path)
    _spend("nous", 4.0, led.path)

    led.check("openai", 1.99, total_cap=10.0, provider_caps={})          # 9.99 fits
    with pytest.raises(BudgetExhausted) as exc:
        led.check("openai", 2.01, total_cap=10.0, provider_caps={})
    assert exc.value.cap == 10.0 and exc.value.provider == "openai"

    with pytest.raises(BudgetExhausted) as exc:
        led.check("nous", 1.5, total_cap=10.0, provider_caps={"nous": 5})
    assert exc.value.cap == 5.0


def test_unreadable_ledger_fails_closed(tmp_path):
    path = tmp_path / "l.json"
    path.write_text("{not json")
    with pytest.raises(BudgetExhausted, match="unreadable"):
        CostLedger(path).check("openai", 0.01, total_cap=10.0, provider_caps={})


def test_unknown_model_is_priced_high_not_free():
    usd = costs.price_tokens("nous/Some-Unlisted-Model", 1_000_000, 0, None)
    assert usd == pytest.approx(costs.DEFAULT_PRICE_PER_M[0])


# --- Cap enforcement in the failover chain ------------------------------------

def test_cap_blocks_call_that_would_exceed(chain):
    chain(["openai/gpt-4o"])
    _spend("openai", 9.99)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(BudgetExhausted, match="spend cap"):
            complete_with_failover("sys", "user " * 2000)
        comp.assert_not_called()          # refused BEFORE any network call
    assert costs.get_ledger().calls() == 1  # nothing new recorded


def test_metered_call_is_recorded_and_bounded(chain):
    chain(["openai/gpt-4o"])
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        comp.return_value = _ok(prompt_tokens=1000, completion_tokens=1000)
        out, model = complete_with_failover("sys", "user")
    assert (out, model) == ("# Notes", "openai/gpt-4o")
    # max_tokens is passed so the pre-call estimate is a true upper bound.
    assert comp.call_args.kwargs["max_tokens"] == settings.metered_max_output_tokens
    [entry] = costs.get_ledger().entries()
    assert entry["provider"] == "openai" and entry["kind"] == costs.METERED
    assert (entry["input_tokens"], entry["output_tokens"]) == (1000, 1000)
    assert entry["usd"] == pytest.approx(0.0125)   # gpt-4o: $2.50/M in, $10/M out
    assert entry["estimated"] is False


def test_missing_usage_charges_the_estimate(chain):
    chain(["openai/gpt-4o"])
    resp = MagicMock()
    resp.choices[0].message.content = "# Notes"
    resp.usage = None
    with patch("video2mdnotes.core.summarizer.litellm.completion", return_value=resp):
        complete_with_failover("sys", "user")
    [entry] = costs.get_ledger().entries()
    assert entry["estimated"] is True
    assert entry["usd"] > 0.1    # >= 16k output tokens at gpt-4o prices


def test_provider_cap_falls_through_to_next_metered(chain, monkeypatch):
    chain(["openai/gpt-4o", "nous/Hermes-4-405B"])
    monkeypatch.setattr(settings, "provider_usd_caps", {"openai": 1.0, "nous": 5.0})
    _spend("openai", 1.0)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        comp.return_value = _ok()
        out, model = complete_with_failover("sys", "user")
    assert model == "nous/Hermes-4-405B"
    comp.assert_called_once()
    kw = comp.call_args.kwargs
    assert kw["model"] == "openai/Hermes-4-405B"
    assert kw["api_base"] == "https://inference-api.nousresearch.com/v1"
    assert kw["api_key"] == "nous-test-key"
    assert costs.get_ledger().spent("nous") > 0


def test_all_metered_capped_stops_cleanly(chain, monkeypatch):
    chain(["openai/gpt-4o", "nous/Hermes-4-405B"])
    monkeypatch.setattr(settings, "provider_usd_caps", {"openai": 1.0, "nous": 1.0})
    _spend("openai", 1.0)
    _spend("nous", 1.0)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(BudgetExhausted) as exc:
            complete_with_failover("sys", "user")
        comp.assert_not_called()
    msg = str(exc.value)
    assert "Stopping" in msg and "openai/gpt-4o" in msg and "nous/Hermes-4-405B" in msg


def test_subscription_never_blocked_by_cap(chain, monkeypatch):
    chain(["claude-cli/opus", "openai/gpt-4o"])
    monkeypatch.setattr(settings, "metered_usd_cap", 0.0)
    _spend("openai", 50.0)
    with patch("video2mdnotes.core.summarizer.subprocess.run", return_value=_cli_ok()), \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        out, model = complete_with_failover("sys", "user")
        comp.assert_not_called()
    assert model == "claude-cli/opus"
    led = costs.get_ledger()
    assert led.calls(kind=costs.SUBSCRIPTION) == 1
    assert led.spent() == pytest.approx(50.0)   # the $0 subscription call added nothing


# --- Exhaustion x metered fallback ------------------------------------------

def test_metered_fallback_off_keeps_todays_behaviour(chain, monkeypatch):
    chain(["claude-cli/opus", "openai/gpt-4o"])
    monkeypatch.setattr(settings, "allow_metered_fallback", False)
    with patch("video2mdnotes.core.summarizer.subprocess.run", return_value=_cli_exhausted()), \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(SubscriptionExhausted):
            complete_with_failover("sys", "user")
        comp.assert_not_called()


def test_metered_fallback_on_escalates_within_cap(chain, monkeypatch):
    chain(["claude-cli/opus", "openai/gpt-4o"])
    monkeypatch.setattr(settings, "allow_metered_fallback", True)
    with patch("video2mdnotes.core.summarizer.subprocess.run", return_value=_cli_exhausted()), \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        comp.return_value = _ok()
        out, model = complete_with_failover("sys", "user")
    assert model == "openai/gpt-4o"


def test_metered_fallback_capped_returns_to_waiting(chain, monkeypatch):
    """Subscription exhausted AND paid caps spent -> wait for reset, not spend."""
    chain(["claude-cli/opus", "openai/gpt-4o"])
    monkeypatch.setattr(settings, "allow_metered_fallback", True)
    _spend("openai", 10.0)
    with patch("video2mdnotes.core.summarizer.subprocess.run", return_value=_cli_exhausted()), \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(SubscriptionExhausted):
            complete_with_failover("sys", "user")
        comp.assert_not_called()


def test_metered_fallback_does_not_override_local_policy(chain, monkeypatch):
    chain(["claude-cli/opus", "openai/gpt-4o"])
    monkeypatch.setattr(settings, "allow_metered_fallback", True)
    monkeypatch.setattr(settings, "on_exhaustion", "local")
    with patch("video2mdnotes.core.summarizer.subprocess.run", return_value=_cli_exhausted()), \
         patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(SubscriptionExhausted):
            complete_with_failover("sys", "user")
        comp.assert_not_called()


# --- Routing ------------------------------------------------------------------

def test_nous_route_uses_configured_base_and_key(monkeypatch):
    monkeypatch.setattr(settings, "nous_api_key", "nous-test-key")
    r = resolve_route("nous/Hermes-4-405B")
    assert r.kind == costs.METERED and r.ledger_provider == "nous"
    assert r.litellm_model == "openai/Hermes-4-405B"
    assert r.api_base == "https://inference-api.nousresearch.com/v1"
    assert r.api_key == "nous-test-key"

    monkeypatch.setattr(settings, "nous_api_base", "http://example.invalid/v1")
    assert resolve_route("nous/Hermes-4-70B").api_base == "http://example.invalid/v1"


def test_nous_without_key_is_skipped_not_called(chain, monkeypatch):
    chain(["nous/Hermes-4-405B"])
    monkeypatch.setattr(settings, "nous_api_key", "your_key_here")   # placeholder
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(RuntimeError, match="no API key"):
            complete_with_failover("sys", "user")
        comp.assert_not_called()


def test_hermes_proxy_route():
    r = resolve_route("hermes/Hermes-4-70B")
    assert r.litellm_model == "openai/Hermes-4-70B"
    assert r.api_base == "http://127.0.0.1:8645/v1"
    assert r.api_key == summarizer.HERMES_PROXY_DUMMY_KEY   # proxy attaches the real one
    assert r.ledger_provider == "nous"                     # billed against the Nous cap
    assert r.kind == costs.METERED


def test_openai_route_untouched_by_nous_settings():
    r = resolve_route("openai/gpt-4o")
    assert r.litellm_model == "openai/gpt-4o" and r.api_base is None
    assert resolve_route("claude-cli/opus").kind == costs.SUBSCRIPTION


def test_caps_parse_from_environment(monkeypatch):
    monkeypatch.setenv("PROVIDER_USD_CAPS", '{"openai": 10, "nous": 5}')
    monkeypatch.setenv("METERED_USD_CAP", "7.5")
    monkeypatch.setenv("ALLOW_METERED_FALLBACK", "true")
    s = Settings(_env_file=None)
    assert s.provider_usd_caps == {"openai": 10.0, "nous": 5.0}
    assert s.metered_usd_cap == 7.5
    assert s.allow_metered_fallback is True
    d = Settings(_env_file=None)
    monkeypatch.delenv("ALLOW_METERED_FALLBACK")
    assert Settings(_env_file=None).allow_metered_fallback is False
    assert d.cost_ledger_path.name == "_cost_ledger.json"
