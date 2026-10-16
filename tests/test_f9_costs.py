"""WP-F9 (b, d, e, g): resume after a wait, billed-but-failed calls, atomic
cap reservation across threads, and the anthropic/* refusal."""

import threading
from unittest.mock import MagicMock, patch

import pytest

from video2mdnotes.config import settings
from video2mdnotes.core import costs
from video2mdnotes.core import summarizer
from video2mdnotes.core.summarizer import Route, _complete_metered


def _route(entry="openai/gpt-4o"):
    return Route(entry, costs.METERED, entry.split("/", 1)[0], entry, api_key="k")


def _ok(text="ok"):
    resp = MagicMock()
    resp.choices[0].message.content = text
    resp.usage = None            # no usage -> charged at the estimate
    return resp


# --- (b) --------------------------------------------------------------------------


def test_longform_wait_retry_resumes(monkeypatch, tmp_path):
    from video2mdnotes import main
    from video2mdnotes.core import longform
    from video2mdnotes.core.summarizer import SubscriptionExhausted

    calls = []

    def fake_run(run_dir, **kw):
        calls.append(kw.get("resume"))
        if len(calls) == 1:
            raise SubscriptionExhausted("limit")
        return "done"

    monkeypatch.setattr(longform, "run_longform", fake_run)
    monkeypatch.setattr(main, "_resolve_exhaustion_policy", lambda: "wait")
    monkeypatch.setattr(main, "_wait_for_reset", lambda exc: None)
    assert main._longform_waiting(tmp_path, resume=False, progress=lambda m: None) == "done"
    assert calls == [False, True]


# --- (d) --------------------------------------------------------------------------


def test_empty_output_after_billing_is_recorded(monkeypatch):
    settings.metered_usd_cap = 10.0
    with patch("video2mdnotes.core.summarizer.litellm.completion", return_value=_ok("")):
        with pytest.raises(ValueError, match="empty"):
            _complete_metered(_route(), "sys", "user text")
    entries = costs.get_ledger().entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == costs.METERED and e["usd"] > 0 and e["estimated"] is True
    assert "reserved" not in e and "failed after send" in e["note"]


def test_timeout_after_send_is_recorded():
    settings.metered_usd_cap = 10.0
    with patch("video2mdnotes.core.summarizer.litellm.completion",
               side_effect=TimeoutError("read timed out")):
        with pytest.raises(TimeoutError):
            _complete_metered(_route(), "sys", "user text")
    assert costs.get_ledger().spent("openai") > 0


def test_rejected_request_is_not_charged():
    settings.metered_usd_cap = 10.0
    err_cls = summarizer._UNBILLED_ERRORS[0]
    err = err_cls.__new__(err_cls)
    Exception.__init__(err, "bad key")
    with patch("video2mdnotes.core.summarizer.litellm.completion", side_effect=err):
        with pytest.raises(err_cls):
            _complete_metered(_route(), "sys", "user text")
    assert costs.get_ledger().entries() == []


# --- (e) --------------------------------------------------------------------------


def test_cap_reservation_is_atomic_across_threads(monkeypatch):
    """Three chapter threads, cap room for one call: exactly one may proceed.

    Before F9 each thread passed `check` against the same spent total (record
    only happened after the call), so all three ran and the cap was overshot.
    """
    estimate = costs.price_tokens("openai/gpt-4o", 10, settings.metered_max_output_tokens,
                                  "openai/gpt-4o")
    settings.metered_usd_cap = estimate * 1.5
    barrier = threading.Barrier(3, timeout=5)
    results = []

    def slow_completion(**kw):
        try:
            barrier.wait()          # all callers that got through wait here
        except threading.BrokenBarrierError:
            pass
        return _ok()

    monkeypatch.setattr(costs, "estimate_prompt_tokens", lambda *a, **k: 10)

    def worker():
        try:
            _complete_metered(_route(), "s", "u")
            results.append("called")
        except costs.BudgetExhausted:
            results.append("refused")
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass

    with patch("video2mdnotes.core.summarizer.litellm.completion", side_effect=slow_completion):
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
    assert sorted(results) == ["called", "refused", "refused"]
    assert costs.get_ledger().spent() <= settings.metered_usd_cap


def test_settle_replaces_reservation_with_real_cost():
    led = costs.get_ledger()
    rid = led.reserve(provider="openai", model="openai/gpt-4o", estimate_usd=1.0,
                      total_cap=10.0, provider_caps={})
    assert led.spent() == pytest.approx(1.0)
    led.settle(rid, input_tokens=5, output_tokens=7, usd=0.01, estimated=False)
    assert led.spent() == pytest.approx(0.01)
    assert "reserved" not in led.entries()[0]


# --- (g) --------------------------------------------------------------------------


def test_anthropic_route_refused_before_ledger(monkeypatch):
    settings.allow_anthropic_api = False

    def no_ledger():
        raise AssertionError("ledger touched before the anthropic refusal")

    monkeypatch.setattr(costs, "get_ledger", no_ledger)
    with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
        with pytest.raises(costs.AnthropicApiRefused, match="ALLOW_ANTHROPIC_API"):
            _complete_metered(_route("anthropic/claude-haiku-4-5"), "s", "u")
        comp.assert_not_called()


def test_anthropic_route_allowed_only_by_explicit_flag():
    settings.allow_anthropic_api = True
    with patch("video2mdnotes.core.summarizer.litellm.completion", return_value=_ok("hi")):
        assert _complete_metered(_route("anthropic/claude-haiku-4-5"), "s", "u") == "hi"


def test_chain_skips_anthropic_entry(mock_prompt_file=None):
    saved = (settings.llm_models, settings.anthropic_api_key)
    try:
        settings.llm_models = ["anthropic/claude-haiku-4-5"]
        settings.anthropic_api_key = "real-looking-key"
        settings.allow_anthropic_api = False
        with patch("video2mdnotes.core.summarizer.litellm.completion") as comp:
            with pytest.raises(RuntimeError, match="refused"):
                summarizer.complete_with_failover("s", "u")
            comp.assert_not_called()
    finally:
        settings.llm_models, settings.anthropic_api_key = saved


def test_vision_tier_refuses_anthropic(tmp_path):
    from PIL import Image
    from video2mdnotes.core import visuals

    settings.allow_anthropic_api = False
    img = tmp_path / "f.png"
    Image.new("RGB", (8, 8)).save(img)
    with patch("video2mdnotes.core.visuals.litellm.completion") as comp:
        assert visuals.describe_image(img, model="anthropic/claude-opus-4-8") == ""
        comp.assert_not_called()
