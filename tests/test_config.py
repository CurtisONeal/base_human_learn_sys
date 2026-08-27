from video2mdnotes.config import settings

def test_settings_load_defaults():
    """Verify that settings load with expected default values."""
    assert settings.llm_provider == "openai"
    assert settings.fw_model == "large-v3"
    assert settings.fw_lang == "en"

def test_paths_exist():
    """Verify that critical paths are resolved correctly relative to the project root."""
    # The prompt file should exist in the repo
    assert settings.prompt_file.exists()
    assert settings.prompt_file.name == "summarize_prompt.txt"

def test_compute_type_resolution():
    """Verify that 'auto' compute type is resolved to a concrete string."""
    assert settings.fw_compute in ["int8", "float16", "int8_float16", "float32"]
    assert settings.fw_compute != "auto"
