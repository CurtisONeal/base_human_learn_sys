"""Model selection tests.

The policy being pinned here is not an optimisation, it is a correctness rule:
an English-only model must never be chosen for Japanese material, no matter how
much extra English it would recover from the noise around it.
"""

import pytest

from video2mdnotes.core.asr_select import (
    ENGLISH_NOISY, MULTILINGUAL, choose_model, detect_non_english_context,
)


@pytest.mark.parametrize("text,expected_hit", [
    ("2nd Kyu test — shomenuchi ikkyo", "Kyu"),
    ("Aikido seminar with Yamada Sensei", "Aikido"),
    ("iriminage and kotegaeshi drills", "iriminage"),
    ("randori practice at the dojo", "randori"),
    ("learning Japanese grammar", "Japanese"),
    ("Postman API tutorial for beginners", None),
    ("Kubernetes networking deep dive", None),
])
def test_non_english_context_detection(text, expected_hit):
    hit = detect_non_english_context(text)
    assert (hit is None) == (expected_hit is None)
    if expected_hit:
        assert hit.lower() == expected_hit.lower()


def test_japanese_context_beats_noisy_audio(tmp_path, monkeypatch):
    """The rule that matters. Noise must NEVER override language.

    An English-only model on Japanese produces confident phonetic nonsense —
    worse than a slower correct transcription, because it looks like a result.
    """
    import video2mdnotes.core.asr_select as sel
    monkeypatch.setattr(sel, "measure_mean_db", lambda p: -40.0)  # very noisy
    choice = choose_model("Aikido 2nd kyu test", tmp_path / "a.wav")
    assert choice.model == MULTILINGUAL
    assert not choice.is_english_only
    assert "not allowed to override" in choice.reason


def test_noisy_english_uses_the_english_model(tmp_path, monkeypatch):
    import video2mdnotes.core.asr_select as sel
    monkeypatch.setattr(sel, "measure_mean_db", lambda p: -33.0)
    choice = choose_model("camera review", tmp_path / "a.wav")
    assert choice.model == ENGLISH_NOISY


def test_quiet_english_defaults_to_multilingual(tmp_path, monkeypatch):
    """Default is multilingual, assuming close-mic speech."""
    import video2mdnotes.core.asr_select as sel
    monkeypatch.setattr(sel, "measure_mean_db", lambda p: -20.0)
    assert choose_model("camera review", tmp_path / "a.wav").model == MULTILINGUAL


def test_no_audio_defaults_to_multilingual():
    assert choose_model("some talk", None).model == MULTILINGUAL


def test_unmeasurable_audio_does_not_pick_english_only(tmp_path):
    """If loudness cannot be measured, fail safe to multilingual."""
    assert choose_model("some talk", tmp_path / "missing.wav").model == MULTILINGUAL


def test_threshold_is_configurable(tmp_path, monkeypatch):
    """Calibrated on one sample, so it must be tunable rather than baked in."""
    import video2mdnotes.core.asr_select as sel
    monkeypatch.setattr(sel, "measure_mean_db", lambda p: -30.0)
    assert choose_model("x", tmp_path / "a.wav", noisy_threshold_db=-35.0).model == MULTILINGUAL
    assert choose_model("x", tmp_path / "a.wav", noisy_threshold_db=-25.0).model == ENGLISH_NOISY


def test_auto_select_can_be_switched_off(tmp_path, monkeypatch):
    from video2mdnotes.config import settings
    from video2mdnotes.core.transcriber import select_model
    monkeypatch.setattr(settings, "asr_auto_select", False)
    monkeypatch.setattr(settings, "fw_model", "pinned-model")
    model, reason = select_model(tmp_path / "a.wav", "Aikido")
    assert model == "pinned-model" and "disabled" in reason


def test_an_explicit_fw_model_is_honoured_not_overridden(tmp_path, monkeypatch):
    """Auto-select picks BETWEEN two models; anything else is a deliberate pin.

    Regression: auto-select ignored FW_MODEL entirely, so tests that pinned
    "tiny" loaded 3.1 GB large-v3 instead and the suite went from 5 minutes to
    39. It also silently contradicted the documented behaviour.
    """
    from video2mdnotes.config import settings
    from video2mdnotes.core.transcriber import select_model
    monkeypatch.setattr(settings, "fw_model", "tiny")
    model, reason = select_model(tmp_path / "a.wav", "Aikido 2nd kyu")
    assert model == "tiny" and "pinned" in reason


def test_auto_select_still_runs_when_fw_model_is_a_candidate(tmp_path, monkeypatch):
    from video2mdnotes.config import settings
    from video2mdnotes.core.transcriber import select_model
    import video2mdnotes.core.asr_select as sel
    monkeypatch.setattr(settings, "fw_model", "large-v3")
    monkeypatch.setattr(sel, "measure_mean_db", lambda p: -33.0)
    assert select_model(tmp_path / "a.wav", "camera review")[0] == "distil-large-v3"
