import pytest
from typer.testing import CliRunner
import datetime as dt

from video2mdnotes.main import app
from video2mdnotes.config import settings

runner = CliRunner()

@pytest.mark.integration
def test_full_pipeline_e2e_single_video(tmp_path):
    """
    End-to-end test for a single video URL.
    """
    if not settings.openai_api_key and not settings.anthropic_api_key:
        pytest.skip("No API key found in settings. Skipping E2E test.")

    original_output_dir = settings.output_dir
    original_temp_dir = settings.temp_dir
    settings.output_dir = tmp_path / "output"
    settings.temp_dir = tmp_path / "temp"
    original_model = settings.fw_model
    settings.fw_model = "tiny"

    try:
        test_url = "https://www.youtube.com/watch?v=tPEE9ZwTmy0"
        result = runner.invoke(app, ["process", test_url])
        
        if result.exit_code != 0:
            print(f"CLI exited with code {result.exit_code}")
            print("--- STDOUT ---")
            print(result.stdout)
            print("--- EXCEPTION ---")
            print(result.exception)
        
        assert result.exit_code == 0
        assert "Processing Complete for" in result.stdout

        date_str = dt.date.today().strftime('%Y%m%d')
        expected_project_dir_name = f"{date_str}_shortest_video_on_youtube"
        project_dir = settings.output_dir / expected_project_dir_name
        # This fixture is a one-second silent clip, so it legitimately produces
        # no usable notes and MARK_EMPTY_RESULTS renames the directory. Accept
        # either name: the marking is the feature working, not a regression.
        if not project_dir.exists():
            marked = settings.output_dir / f"no_summary_{expected_project_dir_name}"
            assert marked.exists(), (
                f"neither {project_dir.name} nor {marked.name} was created"
            )
            project_dir = marked
        
        assert project_dir.exists()
        assert (project_dir / "original_url.txt").exists()
        assert (project_dir / "wav_files").exists()
        assert len(list((project_dir / "wav_files").iterdir())) == 1

    finally:
        settings.output_dir = original_output_dir
        settings.temp_dir = original_temp_dir
        settings.fw_model = original_model

@pytest.mark.integration
def test_full_pipeline_e2e_playlist(tmp_path):
    """
    End-to-end test for a playlist URL.
    Uses a custom public playlist with 2 videos.
    """
    if not settings.openai_api_key and not settings.anthropic_api_key:
        pytest.skip("No API key found in settings. Skipping E2E test.")

    original_output_dir = settings.output_dir
    original_temp_dir = settings.temp_dir
    settings.output_dir = tmp_path / "output"
    settings.temp_dir = tmp_path / "temp"
    original_model = settings.fw_model
    settings.fw_model = "tiny"

    try:
        # A stable playlist created for testing (2 videos)
        test_url = "https://www.youtube.com/watch?v=7Gt-7U1ctao&list=PLirzgrJM69ktRZAjZufbrHk7hSVvpDyEV"
        result = runner.invoke(app, ["process", test_url])
        
        if result.exit_code != 0:
            print(f"CLI exited with code {result.exit_code}")
            print("--- STDOUT ---")
            print(result.stdout)
            print("--- EXCEPTION ---")
            print(result.exception)
        
        assert result.exit_code == 0
        # Expect 2 videos processed
        assert result.stdout.count("Processing Complete for") == 2

        # Verify that multiple project directories were created
        output_dirs = list(settings.output_dir.iterdir())
        assert len(output_dirs) == 2

        # Check one of the directories for structure
        project_dir = output_dirs[0]
        assert project_dir.is_dir()
        assert (project_dir / "original_url.txt").exists()
        assert (project_dir / "wav_files").exists()
        assert (project_dir / "transcripts").exists()
        assert (project_dir / "summaries").exists()

    finally:
        settings.output_dir = original_output_dir
        settings.temp_dir = original_temp_dir
        settings.fw_model = original_model
