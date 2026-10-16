import re
import datetime as dt
from pathlib import Path
from pydantic import BaseModel
from typing import Any, Dict, List, Optional
import yt_dlp

from video2mdnotes.config import settings
from video2mdnotes.logger import logger
from video2mdnotes.core.embeds import scrape_embed_urls
from video2mdnotes.core.metadata import SourceMeta, from_ytdlp_info

class SourceInfo(BaseModel):
    """Metadata for one video, gathered without downloading any audio.

    Probing is split from fetching so the pipeline can decide whether audio is
    needed at all: when a usable human-authored caption track exists, the audio
    download and the Whisper run are both skipped.
    """
    title: str
    url: str
    tags: List[str] = []
    description: str = ""
    # Human-authored tracks. Machine `automatic_captions` are intentionally NOT
    # carried here — see core/captions.py for why they are not trusted.
    subtitles: Dict[str, List[Dict[str, Any]]] = {}
    has_automatic_captions: bool = False
    # Set when the source URL was a playlist, so a run can group its notes into
    # one directory instead of scattering them by date across the output root.
    playlist_title: Optional[str] = None
    playlist_id: Optional[str] = None
    # Publication facts from the same info dict (upload date, uploader,
    # duration, chapters). Written to source_meta.json by the caller once the
    # run directory exists.
    meta: Optional[SourceMeta] = None


class DownloadResult(BaseModel):
    """
    Represents the result of a successful audio download.
    """
    # None when the transcript came from captions and no audio was fetched.
    audio_path: Optional[Path] = None
    title: str
    url: str
    download_date: dt.date
    tags: List[str] = []
    description: str = ""

def sanitize_filename(name: str) -> str:
    """
    Cleans a string to be a safe filename.
    - Replaces whitespace and dashes with underscores.
    - Converts to lowercase.
    - Removes special characters.
    """
    name = re.sub(r'[\s-]+', '_', name)
    name = name.lower()
    name = re.sub(r'[^a-z0-9_]', '', name)
    return name

def probe_source(url: str, allow_embed_scrape: bool = True) -> List[SourceInfo]:
    """
    Gathers metadata and caption availability WITHOUT downloading any audio.

    Split out from download_audio() so the caller can check for a usable caption
    track before paying for an audio download and a Whisper run.

    If yt-dlp cannot handle the URL at all and SCRAPE_PAGE_EMBEDS is on, the page
    is fetched once and any embedded video URLs are probed instead. That covers
    course pages and blogs whose player is injected by JavaScript, which yt-dlp's
    generic extractor cannot see.

    Args:
        url: The URL of the video, playlist, or containing page to probe.
        allow_embed_scrape: Internal. False when already probing a scraped URL,
            so a page of embeds cannot recurse into another round of scraping.

    Returns:
        A list of SourceInfo objects (one per video; playlists yield many).
    """
    # No player_client override. This used to force the Android client "for
    # playlist reliability", but YouTube now requires a GVS PO Token for that
    # client, so its formats 403 on download — measured 2026-08-16: android
    # exposed 1 usable audio format against 5 on the defaults, and half a real
    # course run failed with HTTP 403 because of it. Playlists were re-verified
    # without it.
    ydl_opts_info = {'quiet': True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts_info) as ydl:
            info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        if not (allow_embed_scrape and settings.scrape_page_embeds):
            raise
        logger.info(f"yt-dlp cannot handle {url} directly; looking for embedded videos.")
        embed_urls = scrape_embed_urls(url)
        if not embed_urls:
            raise e

        sources: List[SourceInfo] = []
        for embed_url in embed_urls:
            try:
                # allow_embed_scrape=False: probe the embed itself, never scrape
                # another page from it.
                sources.extend(probe_source(embed_url, allow_embed_scrape=False))
            except Exception as inner:  # noqa: BLE001 - one bad embed is not fatal
                logger.warning(f"Skipping embedded video {embed_url}: {inner}")
        if not sources:
            raise e
        return sources

    # Determine if the result is a playlist or a single video
    if 'entries' in info:
        entries = info['entries']
        # yt-dlp puts playlist identity on the container, not the entries (and
        # extract_flat drops the per-entry playlist_* keys entirely), so read it
        # here and hand it to every source.
        playlist_title = info.get('title')
        playlist_id = info.get('id')
    else:
        # It's a single video, wrap it in a list to use the same loop
        entries = [info]
        playlist_title = playlist_id = None

    sources: List[SourceInfo] = []
    for index, entry in enumerate(entries):
        # yt-dlp yields None for playlist items that are private, deleted or
        # geo-blocked. Skip them: one unavailable video in a long playlist must
        # not abort the whole run.
        if entry is None:
            logger.warning(f"Skipping unavailable playlist entry at position {index + 1}.")
            continue

        # For playlist entries, the 'url' key is present.
        # For a single video info dict, it might be missing. In that case, we use the original URL.
        sources.append(SourceInfo(
            # Prefer the canonical watch page. `url` on a YouTube entry is a
            # signed CDN link that expires, which made archived original_url.txt
            # files useless for re-running a source later.
            url=entry.get('webpage_url') or entry.get('url') or url,
            title=entry.get('title', 'untitled'),
            tags=entry.get('tags') or [],
            description=entry.get('description') or "",
            subtitles=entry.get('subtitles') or {},
            has_automatic_captions=bool(entry.get('automatic_captions')),
            playlist_title=playlist_title,
            playlist_id=playlist_id,
            meta=from_ytdlp_info(entry),
        ))
    return sources


def fetch_audio(source: SourceInfo, convert: bool = True) -> Path:
    """
    Downloads the audio for one probed source and returns its path.

    convert=False keeps yt-dlp's native audio stream (m4a/webm/opus) with no
    post-processing: the transcription path (core/longasr.py) extracts bounded
    16 kHz windows from it, so a multi-hour talk is never converted whole to
    one wav. convert=True (legacy callers) returns a whole-file .wav.

    Raises:
        yt_dlp.utils.DownloadError: If yt-dlp fails to download the audio.
    """
    settings.temp_dir.mkdir(exist_ok=True)

    sanitized_title = sanitize_filename(source.title)
    logger.info(f"Downloading audio: {source.title}")

    date_str = dt.date.today().strftime('%Y%m%d')
    output_path = settings.temp_dir / f"{date_str}_{sanitized_title}.wav"

    ydl_opts_download = {
        'format': 'bestaudio/best',
        'outtmpl': str(output_path.with_suffix('')) + ('' if convert else '.%(ext)s'),
        'quiet': True,
    }
    if convert:
        ydl_opts_download['postprocessors'] = [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': 'wav'}]

    try:
        with yt_dlp.YoutubeDL(ydl_opts_download) as ydl:
            if convert:
                ydl.download([source.url])
                return output_path
            info = ydl.extract_info(source.url, download=True)
            downloads = (info or {}).get("requested_downloads") or []
            path = downloads[0].get("filepath") if downloads else None
            return Path(path or ydl.prepare_filename(info))
    except yt_dlp.utils.DownloadError as e:
        logger.error(f"Failed to download {source.title}. Reason: {e}")
        # Re-raise to stop the entire process if one video fails
        raise e


def download_audio(url: str) -> List[DownloadResult]:
    """
    Probes a URL and downloads audio for every video it yields.

    Retained as the unconditional download path (and for callers that always
    want audio). The captions-first pipeline uses probe_source() + fetch_audio()
    directly so it can skip this entirely.
    """
    results: List[DownloadResult] = []
    for source in probe_source(url):
        logger.info(f"Processing video: {source.title}")
        results.append(DownloadResult(
            audio_path=fetch_audio(source),
            title=source.title,
            url=source.url,
            download_date=dt.date.today(),
            tags=source.tags,
            description=source.description,
        ))
    return results
