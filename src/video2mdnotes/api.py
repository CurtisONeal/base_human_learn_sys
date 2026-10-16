from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl

from video2mdnotes.config import settings
from video2mdnotes.logger import logger
from video2mdnotes.core.downloader import fetch_audio, probe_source
from video2mdnotes.core.transcriber import build_initial_prompt
from video2mdnotes.core.summarizer import generate_summary
from video2mdnotes.core.captions import transcript_from_captions
import shutil
import datetime as dt

app = FastAPI(
    title="Video2MDNotes API",
    description="API for processing video URLs to generate markdown notes.",
)

# Configure CORS to allow requests from the Chrome Extension
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins (safe for local dev tools)
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)

class ProcessRequest(BaseModel):
    url: HttpUrl

def process_pipeline(url: str):
    """
    The core processing pipeline, designed to be run in the background.
    This is almost identical to the CLI's process function.
    """
    logger.info(f"API: Starting background processing for URL: {url}")
    try:
        sources = probe_source(url)
        logger.success(f"API: Found {len(sources)} videos.")

        for source in sources:
            logger.info(f"API: Processing: {source.title}")

            # Captions first; Whisper only when no usable manual track exists.
            transcript_result = None
            if settings.captions_first:
                transcript_result = transcript_from_captions(
                    title=source.title, subtitles=source.subtitles
                )

            from video2mdnotes.core.downloader import sanitize_filename
            safe_title = sanitize_filename(source.title)
            date_str = dt.date.today().strftime('%Y%m%d')
            project_dir_name = f"{date_str}_{safe_title}"
            project_dir = settings.output_dir / project_dir_name

            audio_path = None
            if transcript_result is None:
                # Same path as the CLI: windowed ASR over the native download,
                # never one whole-file wav.
                from video2mdnotes.core import longasr
                audio_path = fetch_audio(source, convert=False)
                initial_prompt = build_initial_prompt(
                    title=source.title,
                    tags=source.tags,
                    description=source.description
                )
                transcript_result = longasr.transcribe_media(
                    [audio_path], run_dir=project_dir, title=source.title,
                    metadata_text=f"{source.title} {source.description or ''}",
                    initial_prompt=initial_prompt, progress=logger.info,
                ).result
            logger.info(f"API: Transcript via {transcript_result.transcript_source}")

            summary_result = generate_summary(transcript_result)

            # Archiving logic (same as CLI)
            project_dir.mkdir(parents=True, exist_ok=True)

            transcripts_dir = project_dir / "transcripts"
            summaries_dir = project_dir / "summaries"

            transcripts_dir.mkdir(exist_ok=True)
            summaries_dir.mkdir(exist_ok=True)

            # No audio exists on the captions path — nothing to archive.
            if audio_path is not None:
                wav_dir = project_dir / "wav_files"
                wav_dir.mkdir(exist_ok=True)
                shutil.move(str(audio_path), str(wav_dir / audio_path.name))

            (transcripts_dir / f"{safe_title}.md").write_text(transcript_result.markdown_content, encoding="utf-8")
            (summaries_dir / f"{safe_title}.summary.md").write_text(summary_result.summary_text, encoding="utf-8")
            (project_dir / "original_url.txt").write_text(str(source.url), encoding="utf-8")

            logger.success(f"API: Successfully processed and archived {source.title}")

    except Exception as e:
        logger.error(f"API: Background processing failed for {url}. Reason: {e}")

@app.post("/process", status_code=202)
def process_video_url(request: ProcessRequest, background_tasks: BackgroundTasks):
    """
    Accepts a video URL and processes it in the background.
    """
    background_tasks.add_task(process_pipeline, str(request.url))
    return {"message": "Processing started in the background. Check logs for progress."}

@app.get("/")
def read_root():
    return {"message": "Welcome to the Video2MDNotes API. Go to /docs to see the API documentation."}
