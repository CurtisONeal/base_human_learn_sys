import typer
import re
import shutil
import sys
import time
import datetime as dt
from pathlib import Path
from rich.console import Console
from rich.panel import Panel

from video2mdnotes.config import settings
from video2mdnotes.logger import logger
from video2mdnotes.core.downloader import DownloadResult, fetch_audio, probe_source
from video2mdnotes.core.transcriber import transcribe_audio, build_initial_prompt
from video2mdnotes.core.summarizer import (
    EMPTY_TRANSCRIPT_PLACEHOLDER,
    SubscriptionExhausted,
    generate_summary,
)
from video2mdnotes.core.captions import transcript_from_captions
from video2mdnotes.core import frames as frames_mod
from video2mdnotes.core import visuals
from video2mdnotes.core import profiles as profiles_mod

app = typer.Typer(help="Video to Markdown Notes Pipeline")
console = Console()

def _resolve_exhaustion_policy() -> str:
    """Current exhaustion policy, resolving "ask" once by prompting.

    The answer is written back to settings so the rest of the run follows it
    without prompting per video. If there is no TTY (a scheduled or agent-driven
    run), "ask" degrades to "wait" — never to spending money, because nobody is
    there to consent.
    """
    policy = (settings.on_exhaustion or "wait").strip().lower()
    if policy != "ask":
        return policy

    if not sys.stdin.isatty():
        logger.warning("--on-exhaustion=ask with no TTY; waiting for reset instead.")
        settings.on_exhaustion = "wait"
        return "wait"

    console.print("[bold yellow]Subscription capacity exhausted.[/bold yellow]")
    choice = typer.prompt(
        "Continue on [m]etered (costs money), [l]ocal, [w]ait for reset, or [s]top?",
        default="w",
    ).strip().lower()[:1]
    settings.on_exhaustion = {
        "m": "metered", "l": "local", "s": "stop"
    }.get(choice, "wait")
    return settings.on_exhaustion


NO_SUMMARY_PREFIX = "no_summary_"


def _mark_empty(project_dir: Path) -> Path:
    """Rename a project directory to flag that it produced no usable notes."""
    if project_dir.name.startswith(NO_SUMMARY_PREFIX):
        return project_dir
    marked = project_dir.with_name(f"{NO_SUMMARY_PREFIX}{project_dir.name}")
    if marked.exists():
        shutil.rmtree(marked, ignore_errors=True)
    project_dir.rename(marked)
    return marked


def _transcript_segments(project_dir: Path):
    """Recover segment timings from an archived transcript.

    The visuals-only pass has no TranscriptResult in memory, but the archived
    transcript markdown carries `- [H:MM:SS.mmm] text` lines — enough to restore
    the timings the ranker and the narration quotes need.
    """
    from video2mdnotes.core.transcriber import Segment

    files = list((project_dir / "transcripts").glob("*.md"))
    if not files:
        return []
    pattern = re.compile(r"^- \[(\d+):(\d{2}):(\d{2})\.(\d+)\]\s*(.*)$")
    segments = []
    for line in files[0].read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        h, mi, sec, ms, text = m.groups()
        start = int(h) * 3600 + int(mi) * 60 + int(sec) + int(ms) / 1000
        segments.append(Segment(start=start, end=start + 5.0, text=text))
    return segments


def _add_visuals(project_dir: Path, source, summary_path: Path) -> None:
    """Add a visual section to notes that already exist, without re-transcribing.

    Reads the transcript back off disk to recover the segment timings the
    ranker uses for audio cues, so a separate pass ranks frames the same way
    the inline path would.
    """
    from video2mdnotes.core import frames as frames_mod

    video_file = frames_mod.download_video(source.url, project_dir / "_video")
    if not video_file:
        raise RuntimeError("could not download video for frame extraction")
    try:
        keyframes = frames_mod.extract_and_dedupe(video_file, project_dir / "frames")
        if not keyframes:
            logger.info(f"No keyframes extracted for {source.title}")
            return
        segments = _transcript_segments(project_dir)
        readings = visuals.read_frames(keyframes, segments=segments)
        visual_markdown = visuals.render_markdown(readings, segments=segments)
        if not visual_markdown:
            logger.info(f"Frames carried no readable content for {source.title}")
            return

        summary_path.write_text(
            visuals.insert_visual_section(
                summary_path.read_text(encoding="utf-8"), visual_markdown
            ),
            encoding="utf-8",
        )
        logger.success(f"Added {len(readings)} visual(s) to {summary_path.name}")

        # The notes may now be usable even though the audio produced nothing.
        marked = project_dir.name.startswith(NO_SUMMARY_PREFIX)
        if marked and visuals.has_usable_notes(summary_path.read_text(encoding="utf-8")):
            unmarked = project_dir.with_name(project_dir.name[len(NO_SUMMARY_PREFIX):])
            project_dir.rename(unmarked)
            logger.success(f"Visuals rescued empty notes — renamed to {unmarked.name}")
    finally:
        video_file.unlink(missing_ok=True)
        shutil.rmtree(project_dir / "_video", ignore_errors=True)


def _project_root(source) -> Path:
    """Output root for one source, grouped by playlist when asked.

    Grouping is opt-in because turning it on moves where notes land, and a
    library that silently reorganises itself between runs is worse than one
    that scatters predictably.
    """
    from video2mdnotes.core.downloader import sanitize_filename

    root = settings.output_dir
    if settings.group_by_playlist and getattr(source, "playlist_title", None):
        root = root / sanitize_filename(source.playlist_title)
    return root


def _existing_summary(safe_title: str, root: Path | None = None) -> Path | None:
    """Find an already-generated summary for this video, if any.

    Matched on title independent of run date, because the project directory is
    named `<date>_<title>` and a resume the next day must still recognise work
    finished today.
    """
    root = root or settings.output_dir
    # Search the grouped directory and the output root, so notes made before
    # --group-by-playlist existed are still found and not silently redone.
    for base in {root, settings.output_dir}:
        if not base.exists():
            continue
        for candidate in base.glob(f"*_{safe_title}"):
            summary = candidate / "summaries" / f"{safe_title}.summary.md"
            if summary.exists() and summary.stat().st_size > 0:
                return summary
    return None


def _wait_for_reset(exhausted: SubscriptionExhausted) -> None:
    """Sleep until the subscription limit resets, then let the caller retry."""
    now = dt.datetime.now()
    if exhausted.reset_at and exhausted.reset_at > now:
        seconds = (exhausted.reset_at - now).total_seconds()
        source = f"reset time reported by the CLI ({exhausted.reset_at:%Y-%m-%d %H:%M})"
    else:
        seconds = settings.exhaustion_wait_seconds
        source = "no reset time reported — using EXHAUSTION_WAIT_SECONDS"

    seconds = min(seconds, settings.exhaustion_max_wait_seconds)
    resume_at = now + dt.timedelta(seconds=seconds)

    console.print(Panel(
        f"[bold yellow]Subscription capacity exhausted.[/bold yellow]\n\n"
        f"Not falling back to a paid backend — waiting for capacity you already\n"
        f"pay for. Override with [bold]--on-exhaustion metered[/bold] (or "
        f"[bold]local[/bold]/[bold]stop[/bold]).\n\n"
        f"{source}\n"
        f"Waiting {seconds/3600:.1f}h, resuming about {resume_at:%H:%M}.\n\n"
        f"Safe to Ctrl-C: finished videos are on disk and will be skipped when\n"
        f"you re-run the same command.",
        title="Waiting for limit reset",
        border_style="yellow",
    ))
    logger.warning(f"Subscription exhausted; sleeping {seconds:.0f}s until ~{resume_at:%H:%M}.")
    time.sleep(seconds)


@app.command()
def media(
    path: str = typer.Argument(..., help="Audio/video file, or a directory of them"),
    force: bool = typer.Option(False, "--force", help="Re-transcribe files that already have notes"),
    probe_only: bool = typer.Option(
        False, "--probe", help="Report format, duration and codec. Free — no transcription."
    ),
    keep_wav: bool = typer.Option(False, "--keep-wav", help="Keep the converted 16kHz wav"),
    on_exhaustion: str = typer.Option(None, "--on-exhaustion"),
):
    """Transcribe and summarize local audio or video. No network access.

    Handles what consumer voice recorders actually emit, not just tidy wavs:
    ADPCM inside a .wav, WMA from Windows-era devices, AMR, Opus, or the audio
    track of an .mp4. The format is read from the file itself — a recorder that
    writes ADPCM into a .wav is common, and trusting the extension is how those
    files get silently mishandled.

        media ~/Recordings --probe        # what have I actually got?
        media ~/Recordings/meeting.m4a
    """
    from video2mdnotes.core.media import (
        MediaError, describe, find_media, probe as probe_media, to_wav,
    )
    from video2mdnotes.core.downloader import sanitize_filename

    if on_exhaustion:
        settings.on_exhaustion = on_exhaustion.strip().lower()

    target = Path(path).expanduser()
    if not target.exists():
        console.print(f"[red]No such path:[/red] {target}")
        raise typer.Exit(1)

    files = find_media(target)
    if not files:
        console.print(f"[red]No supported media in[/red] {target}")
        console.print("[dim]If your recorder writes a proprietary container, export "
                      "to WAV or MP3 from its own software first.[/dim]")
        raise typer.Exit(1)

    done = failed = skipped = 0
    for position, source_file in enumerate(files, 1):
        label = f"[{position}/{len(files)}]"
        try:
            info = probe_media(source_file)
        except MediaError as exc:
            console.print(f"{label} [red]{type(exc).__name__}:[/red] {exc}")
            logger.error(f"{source_file.name}: {exc}")
            failed += 1
            continue
        except FileNotFoundError:
            console.print(f"{label} [red]vanished:[/red] {source_file}")
            failed += 1
            continue

        console.print(f"{label} [bold]{source_file.name}[/bold]")
        console.print(f"      [dim]{describe(info)}[/dim]")
        if probe_only:
            continue

        safe_title = sanitize_filename(source_file.stem)
        project_dir = settings.output_dir / safe_title
        summary_path = project_dir / "summaries" / f"{safe_title}.summary.md"
        if summary_path.exists() and not force:
            console.print("      [dim]Skipping (already summarized)[/dim]")
            skipped += 1
            continue

        wav_dir = project_dir / "audio"
        wav_path = wav_dir / f"{safe_title}.wav"
        try:
            to_wav(source_file, wav_path, info)
        except MediaError as exc:
            console.print(f"      [red]Conversion failed:[/red] {exc}")
            failed += 1
            continue

        try:
            transcript = transcribe_audio(wav_path, title=source_file.stem)
        except Exception as exc:  # noqa: BLE001
            console.print(f"      [red]Transcription failed:[/red] {exc}")
            logger.error(f"{source_file.name}: {exc}")
            failed += 1
            continue
        finally:
            if not keep_wav:
                wav_path.unlink(missing_ok=True)

        # The transcript is the expensive artifact — write it BEFORE
        # summarizing, so a summarizer failure never costs the transcription.
        (project_dir / "transcripts").mkdir(parents=True, exist_ok=True)
        transcript_path = project_dir / "transcripts" / f"{safe_title}.md"
        transcript_path.write_text(transcript.markdown_content, encoding="utf-8")
        console.print(f"      [green]Transcript:[/green] {transcript_path}")

        try:
            result = generate_summary(transcript)
        except SubscriptionExhausted as exc:
            if _resolve_exhaustion_policy() == "wait":
                _wait_for_reset(exc)
                try:
                    result = generate_summary(transcript)
                except Exception as retry_exc:  # noqa: BLE001
                    console.print(f"      [red]Failed after wait:[/red] {retry_exc}")
                    failed += 1
                    continue
            else:
                console.print(f"      [red]Stopping:[/red] {exc}")
                failed += 1
                break
        except Exception as exc:  # noqa: BLE001
            console.print(f"      [red]Summary failed:[/red] {exc} "
                          "[dim](transcript kept)[/dim]")
            failed += 1
            continue

        summaries_dir = project_dir / "summaries"
        summaries_dir.mkdir(parents=True, exist_ok=True)
        if EMPTY_TRANSCRIPT_PLACEHOLDER in result.summary_text:
            summary_path = summaries_dir / f"{NO_SUMMARY_PREFIX}{safe_title}.summary.md"
        summary_path.write_text(result.summary_text, encoding="utf-8")
        console.print(f"      [green]Notes:[/green] {summary_path}")
        done += 1

    if probe_only:
        console.print(f"\n[bold]Probed {len(files)} files[/bold] · {failed} unreadable")
    else:
        console.print(f"\n[bold]Media:[/bold] {done} done · {skipped} skipped · {failed} failed")


@app.command()
def catalog(
    action: str = typer.Argument("overview", help="overview | index | embed | search | list | find | toc | verify | relocate | volumes"),
    query: str = typer.Argument(None, help="search term (find) or title fragment (toc)"),
    corpus: str = typer.Option(None, "--corpus", help="Corpus directory (index only)"),
    db: str = typer.Option(None, "--db", help="Catalog database path"),
    force: bool = typer.Option(False, "--force", help="Re-index unchanged files"),
    order: str = typer.Option("title", "--order", help="list: title | words | sections | code"),
    limit: int = typer.Option(40, "--limit"),
    no_semantic: bool = typer.Option(False, "--no-semantic", help="search: lexical only"),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="search: skip cross-encoder"),
):
    """Browse what you own. Free — makes no model call and no network access.

    The catalog is the cheap filter in front of the expensive stage, the same
    way captions gate Whisper and OCR gates the vision model. Cataloguing 396
    books costs minutes; summarizing them costs hundreds of hours.

        catalog index --corpus ~/Documents/corpus
        catalog overview
        catalog find "graph rag"
        catalog list --order code
        catalog toc "Mastering Retrieval"
    """
    from video2mdnotes.core.corpus import (
        book_toc, find_headings, index as build_index, list_books, overview,
    )

    db_path = Path(db) if db else settings.output_dir / "corpus" / "catalog.db"

    if action == "index":
        root = Path(corpus).expanduser() if corpus else None
        if not root or not root.exists():
            console.print("[red]--corpus must point at an existing directory[/red]")
            raise typer.Exit(1)
        console.print(f"Indexing [bold]{root}[/bold] → {db_path}")
        report = build_index(
            root, db_path, force=force,
            progress=lambda i, n, name: console.print(
                f"  [dim]{i}/{n}[/dim] {name[:60]}", highlight=False
            ) if i % 25 == 0 or i == n else None,
        )
        console.print(f"\n[green]{report.summary}[/green]")
        if report.failures:
            console.print(f"\n[yellow]{len(report.failures)} unreadable[/yellow] "
                          "(recorded in the catalog, not skipped):")
            for name, reason in list(report.failures.items())[:12]:
                console.print(f"  {name[:46]:48} [dim]{reason[:60]}[/dim]")
        return

    if not db_path.exists():
        console.print(f"[red]No catalog at[/red] {db_path}\n"
                      "Build one: [bold]catalog index --corpus <dir>[/bold]")
        raise typer.Exit(1)

    if action == "volumes":
        from video2mdnotes.core.corpus import volume_status
        for vol in volume_status(db_path):
            colour = "green" if vol.mounted else "yellow"
            console.print(f"  [{colour}]{vol.books:>4}[/{colour}] books  {vol.label}")
        return

    if action == "verify":
        from video2mdnotes.core.corpus import verify
        report = verify(db_path)
        for vol in report.volumes:
            colour = "green" if vol.mounted else "yellow"
            console.print(f"  [{colour}]{vol.books:>4}[/{colour}] books  {vol.label}")
        console.print(f"\n{report.summary}")
        if report.missing_books:
            console.print("\n[red]Missing[/red] (volume is attached but the file is gone):")
            for book in report.missing_books[:20]:
                console.print(f"  {book['title'][:40]:42} [dim]{book['path'][:60]}[/dim]")
            console.print("\nTry: [bold]catalog relocate[/bold] to find them elsewhere.")
        elif report.offline:
            console.print("[dim]Offline books are fine — attach the drive to read them.[/dim]")
        return

    if action == "relocate":
        from video2mdnotes.core.corpus import relocate
        roots = [Path(corpus).expanduser()] if corpus else [
            Path.home() / "Documents", Path.home() / "Downloads", Path("/Volumes"),
        ]
        console.print("Searching: " + ", ".join(str(r) for r in roots))
        moves = relocate(db_path, roots, dry_run=not force)
        if not moves:
            console.print("[green]Nothing to relocate[/green] — no missing book was "
                          "found elsewhere with a matching name and size.")
            return
        for move in moves:
            console.print(f"  {move['title'][:36]:38}\n     [dim]{move['from'][:70]}[/dim]"
                          f"\n  -> [green]{move['to'][:70]}[/green]")
        if force:
            console.print(f"\n[green]Repointed {len(moves)} books.[/green]")
        else:
            console.print(f"\n[yellow]Dry run[/yellow] — {len(moves)} would move. "
                          "Re-run with [bold]--force[/bold] to apply.")
        return

    if action == "embed":
        from video2mdnotes.core.corpus import build_vectors, vector_count
        console.print("Embedding section headings locally — no API, no data leaves "
                      "this machine.")
        written = build_vectors(
            db_path, force=force,
            progress=lambda done, total: console.print(
                f"  [dim]{done}/{total}[/dim]", highlight=False
            ) if done % 5000 == 0 else None,
        )
        console.print(f"[green]{written} new vectors[/green] "
                      f"({vector_count(db_path)} total)")
        return

    if action == "search":
        from video2mdnotes.core.corpus import search as hybrid_search
        if not query:
            console.print("[red]search needs a query[/red]")
            raise typer.Exit(1)
        hits = hybrid_search(
            db_path, query, limit=limit,
            use_semantic=not no_semantic, use_rerank=not no_rerank,
        )
        if not hits:
            console.print(f"Nothing matches [bold]{query}[/bold]. "
                          "That is a real answer: you own no section on it.")
            return
        mark = {"both": "[green]both[/green]", "lexical": "[cyan]lex [/cyan]",
                "semantic": "[magenta]sem [/magenta]"}
        console.print(f"[bold]{len(hits)}[/bold] books for [bold]{query}[/bold]:\n")
        for hit in hits:
            score = (f" r={hit.rerank_score:5.2f}" if hit.rerank_score is not None
                     else f" f={hit.fused_score:.3f}")
            console.print(
                f"  {mark[hit.found_by]} {hit.book[:30]:32} "
                f"[{hit.locator:>10}] {hit.heading_path.split(' > ')[-1][:38]:40}"
                f"{hit.word_count:>6,}w{score}"
            )
        return

    if action == "overview":
        stats = overview(db_path)
        console.print(
            f"[bold]{stats['books']}[/bold] books · "
            f"[bold]{stats['words']:,}[/bold] words · "
            f"{stats['sections']:,} sections · {stats['code_blocks']:,} code blocks"
        )
        console.print(f"  formats: {stats['by_format']}")
        console.print(f"  structure guessed rather than read: {stats['structure_inferred']}")
        if stats["failed"]:
            console.print(f"  [yellow]unreadable: {stats['failed']}[/yellow]")
        console.print("  provenance:")
        for prov, count in stats["by_provenance"].items():
            console.print(f"     {count:>4}  {prov}")

    elif action == "list":
        for row in list_books(db_path, limit=limit, order=order):
            if row["status"] != "ok":
                console.print(f"  [yellow]![/yellow] {row['title'][:44]:46} "
                              f"[dim]{(row['error'] or '')[:44]}[/dim]")
            else:
                flag = " [yellow]~[/yellow]" if row["structure_inferred"] else "  "
                console.print(
                    f"  {row['title'][:42]:44}{flag} {row['fmt']:4} "
                    f"{row['word_count']:>8,}w {row['section_count']:>5} sec "
                    f"{row['code_blocks']:>4} code"
                )

    elif action == "find":
        if not query:
            console.print("[red]find needs a search term[/red]")
            raise typer.Exit(1)
        hits = find_headings(db_path, query, limit=limit)
        if not hits:
            console.print(f"No heading matches [bold]{query}[/bold]. "
                          "That is a real answer: nothing you own gives it a section.")
            return
        console.print(f"[bold]{len(hits)}[/bold] sections whose heading matches "
                      f"[bold]{query}[/bold]:\n")
        for hit in hits:
            console.print(f"  {hit['book'][:30]:32} [{hit['locator']:>10}] "
                          f"{hit['heading_path'][:48]:50} {hit['word_count']:>6,}w")

    elif action == "toc":
        if not query:
            console.print("[red]toc needs a title fragment[/red]")
            raise typer.Exit(1)
        rows = book_toc(db_path, query)
        if not rows:
            console.print(f"No catalogued book matching [bold]{query}[/bold]")
            return
        for row in rows[:limit]:
            console.print(f"  {'  ' * (row['depth'] - 1)}[{row['locator']:>10}] "
                          f"{row['heading_path'].split(' > ')[-1][:52]:54} "
                          f"{row['word_count']:>6,}w")
    else:
        console.print(f"[red]Unknown action[/red] {action!r}. "
                      "Use: overview | index | list | find | toc")
        raise typer.Exit(1)


@app.command()
def book(
    path: str = typer.Argument(..., help="Path to an .epub or .pdf, or a directory of them"),
    force: bool = typer.Option(
        False, "--force", help="Re-summarize books that already have notes"
    ),
    extract_only: bool = typer.Option(
        False, "--extract-only",
        help="Read structure and report it without calling any model. Free.",
    ),
    max_words: int = typer.Option(
        None, "--max-words", help="Words per passage (default 12000)"
    ),
    on_exhaustion: str = typer.Option(
        None, "--on-exhaustion",
        help="When the subscription runs out: wait | metered | local | stop | ask.",
    ),
):
    """Summarize an owned book or paper, preserving chapter/page locators.

    `--extract-only` is the cheap first move and is worth using habitually: it
    prints the structure the extractor found and what a run would cost in
    passages, without calling a model at all. Reading that output is how you
    catch a book whose structure came out wrong *before* paying to summarize it.
    """
    from video2mdnotes.core.documents import SUPPORTED, extract as extract_document
    from video2mdnotes.core.chunking import chunk_sections
    from video2mdnotes.core.summarizer import summarize_document

    if on_exhaustion:
        settings.on_exhaustion = on_exhaustion.strip().lower()

    target = Path(path).expanduser()
    if not target.exists():
        console.print(f"[red]No such path:[/red] {target}")
        raise typer.Exit(1)

    files = (
        sorted(f for f in target.iterdir() if f.suffix.lower() in SUPPORTED)
        if target.is_dir() else [target]
    )
    if not files:
        console.print(f"[red]No {'/'.join(sorted(SUPPORTED))} files in[/red] {target}")
        raise typer.Exit(1)

    out_root = settings.output_dir / "books"
    out_root.mkdir(parents=True, exist_ok=True)
    done = failed = skipped = 0

    for position, source_file in enumerate(files, 1):
        label = f"[{position}/{len(files)}]"
        summary_path = out_root / f"{source_file.stem}.summary.md"
        if summary_path.exists() and not force and not extract_only:
            console.print(f"{label} [dim]Skipping (already summarized):[/dim] {source_file.name}")
            skipped += 1
            continue

        try:
            source = extract_document(source_file)
        except Exception as exc:  # noqa: BLE001 - one bad book must not stop the batch
            console.print(f"{label} [red]{type(exc).__name__}:[/red] {exc}")
            logger.error(f"{source_file.name}: {exc}")
            failed += 1
            continue

        chunks = chunk_sections(
            source.sections, **({"max_words": max_words} if max_words else {})
        )
        warning = " [yellow](structure inferred)[/yellow]" if source.structure_is_inferred else ""
        console.print(
            f"{label} [bold]{source.title}[/bold]{warning}\n"
            f"      {source.word_count:,} words · {len(source.sections)} sections · "
            f"{len(chunks)} passages · {source.provenance}"
        )

        if extract_only:
            for section in source.sections[:12]:
                console.print(f"        [{section.locator}] {' > '.join(section.path)}")
            if len(source.sections) > 12:
                console.print(f"        [dim]… {len(source.sections) - 12} more[/dim]")
            continue

        try:
            result = summarize_document(
                source,
                max_words=max_words,
                progress=lambda i, n, loc: console.print(
                    f"        passage {i}/{n} [{loc}]", highlight=False
                ),
            )
        except SubscriptionExhausted as exc:
            # Same policy as the video path: never silently start spending. Under
            # the default 'wait' policy this sleeps until capacity returns and
            # retries this book; otherwise it stops, and the already-written
            # notes for earlier books remain on disk to resume from.
            if _resolve_exhaustion_policy() == "wait":
                _wait_for_reset(exc)
                console.print(f"      [yellow]Retrying[/yellow] {source_file.name}")
                try:
                    result = summarize_document(source, max_words=max_words)
                except Exception as retry_exc:  # noqa: BLE001
                    console.print(f"      [red]Failed after wait:[/red] {retry_exc}")
                    failed += 1
                    continue
            else:
                console.print(f"      [red]Stopping:[/red] {exc}")
                failed += 1
                break
        except Exception as exc:  # noqa: BLE001
            console.print(f"      [red]Failed:[/red] {exc}")
            logger.error(f"{source_file.name}: {exc}")
            failed += 1
            continue

        # A book that produced nothing is marked the same way an empty video is,
        # so a directory listing shows which runs are worthless without opening them.
        if EMPTY_TRANSCRIPT_PLACEHOLDER in result.summary_text:
            summary_path = summary_path.with_name(NO_SUMMARY_PREFIX + summary_path.name)
        summary_path.write_text(result.summary_text, encoding="utf-8")
        console.print(f"      [green]Notes:[/green] {summary_path}")
        done += 1

    console.print(
        f"\n[bold]Books:[/bold] {done} summarized · {skipped} skipped · {failed} failed"
    )


@app.command()
def process(
    url: str = typer.Argument(..., help="The URL of the video or playlist to process"),
    keep_wav: bool = typer.Option(True, help="Keep the downloaded WAV file in the archive"),
    force: bool = typer.Option(
        False, "--force",
        help="Re-summarize videos that already have a summary (default: skip them)",
    ),
    group_by_playlist: bool = typer.Option(
        False, "--group-by-playlist",
        help="Put all of a playlist's notes in one directory named after the playlist.",
    ),
    visuals_only: bool = typer.Option(
        False, "--visuals-only",
        help="Add visual content to notes that already exist, without re-transcribing.",
    ),
    profile: str = typer.Option(
        None, "--profile",
        help="Content shape: animated | lecture | screencast | slides | talking_head. "
             "Overrides the source map and the probe.",
    ),
    on_exhaustion: str = typer.Option(
        None, "--on-exhaustion",
        help="When the subscription runs out: wait | metered | local | stop | ask. "
             "Default 'wait' — never starts spending money unattended.",
    ),
):
    """
    Downloads, transcribes, and summarizes a video or playlist from a URL.
    """
    if on_exhaustion:
        settings.on_exhaustion = on_exhaustion.strip().lower()
    if group_by_playlist:
        settings.group_by_playlist = True
    if visuals_only:
        # The whole point of this mode is to add visuals, so enabling it
        # implicitly beats failing with "nothing to do".
        settings.extract_frames = True

    logger.info(f"Starting processing for URL: {url}")

    from video2mdnotes.core.downloader import sanitize_filename

    # 1. Probe (metadata + caption availability; no audio yet)
    try:
        logger.info("Step 1: Probing source...")
        sources = probe_source(url)
        logger.success(f"Found {len(sources)} videos.")
    except Exception as e:
        logger.error(f"Processing failed: {e}")
        raise typer.Exit(code=1)

    # A profile mutates settings for one video; snapshot the configured globals
    # so a screencast profile cannot silently leak into the next video's run.
    _profile_defaults = {
        k: getattr(settings, k) for k in (
            "frame_scene_threshold", "frame_dedupe_threshold", "frame_max",
            "vlm_escalate_below_words", "vlm_max_frames", "vlm_model",
            "extract_frames",
        )
    }

    done: list[str] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []
    stopped_at: str | None = None

    for index, source in enumerate(sources, 1):
        position = f"{index}/{len(sources)}"

        # Resume: work already on disk is not redone. Re-running a playlist
        # after an interruption must not re-summarize what it already has —
        # that is the expensive half of the run.
        for _k, _v in _profile_defaults.items():
            setattr(settings, _k, _v)
        try:
            chosen = profiles_mod.resolve(source, profile)
        except ValueError as e:
            logger.error(str(e))
            raise typer.Exit(code=2)
        settings.__dict__['_profile_locked'] = bool(chosen)
        if chosen:
            profiles_mod.apply(chosen)

        project_root = _project_root(source)

        # --visuals-only adds to notes that already exist; it never transcribes.
        if visuals_only:
            existing = _existing_summary(sanitize_filename(source.title), project_root)
            if not existing:
                logger.warning(f"[{position}] No existing notes to add visuals to: {source.title}")
                failed.append((source.title, "no existing notes"))
                continue
            try:
                _add_visuals(existing.parent.parent, source, existing)
                done.append(source.title)
            except Exception as e:  # noqa: BLE001
                logger.error(f"[{position}] Visuals failed: {source.title} — {e}")
                failed.append((source.title, str(e)))
            continue

        already = _existing_summary(sanitize_filename(source.title), project_root)
        if already and not force:
            logger.info(f"[{position}] Skipping (already summarized): {source.title}")
            skipped.append(source.title)
            continue

        # Per-video isolation: one bad video must not abandon the rest of a
        # playlist. Only exhaustion (handled below) stops the whole run.
        try:
            logger.info(f"[{position}] Processing: {source.title}")

            # 2. Transcript — captions first, Whisper as the fallback.
            logger.info("Step 2: Obtaining Transcript...")
            transcript_result = None
            if settings.captions_first:
                transcript_result = transcript_from_captions(
                    title=source.title, subtitles=source.subtitles
                )
                if transcript_result is None and source.has_automatic_captions:
                    logger.info(
                        "Only machine captions available — using Whisper instead "
                        "(auto-captions are ASR without our vocabulary hint)."
                    )

            audio_path = None
            if transcript_result is None:
                audio_path = fetch_audio(source)
                initial_prompt = build_initial_prompt(
                    title=source.title,
                    tags=source.tags,
                    description=source.description
                )
                transcript_result = transcribe_audio(
                    audio_path,
                    title=source.title,
                    initial_prompt=initial_prompt
                )

            download_result = DownloadResult(
                audio_path=audio_path,
                title=source.title,
                url=source.url,
                download_date=dt.date.today(),
                tags=source.tags,
                description=source.description,
            )
            logger.success(
                f"Transcript ready via {transcript_result.transcript_source}: "
                f"{len(transcript_result.segments)} segments"
            )

            # 2b. Archive the transcript NOW, before anything can fail.
            # Whisper is the expensive step; a summarizer failure (exhaustion,
            # provider error) used to discard it entirely and force a full
            # re-transcription on retry. Writing here also makes the transcript
            # a durable artifact that can be re-summarized with a different
            # prompt without touching the audio again.
            safe_title = sanitize_filename(source.title)
            date_str = dt.date.today().strftime('%Y%m%d')
            project_dir = project_root / f"{date_str}_{safe_title}"
            (project_dir / "transcripts").mkdir(parents=True, exist_ok=True)
            transcript_path = project_dir / "transcripts" / f"{safe_title}.md"
            transcript_path.write_text(
                transcript_result.markdown_content, encoding="utf-8"
            )
            (project_dir / "original_url.txt").write_text(source.url, encoding="utf-8")

            # 3. Summarize — retries across a limit reset when policy is "wait".
            logger.info("Step 3: Generating Summary...")
            while True:
                try:
                    summary_result = generate_summary(transcript_result)
                    break
                except SubscriptionExhausted as e:
                    if _resolve_exhaustion_policy() != "wait":
                        raise
                    _wait_for_reset(e)
            logger.success("Summary Generated.")

            # 4. Archive / Output
            logger.info("Step 4: Archiving Results...")
            
            from video2mdnotes.core.downloader import sanitize_filename
            safe_title = sanitize_filename(download_result.title)
            date_str = dt.date.today().strftime('%Y%m%d')
            project_dir_name = f"{date_str}_{safe_title}"
            
            project_dir = project_root / project_dir_name
            project_dir.mkdir(parents=True, exist_ok=True)

            transcripts_dir = project_dir / "transcripts"
            summaries_dir = project_dir / "summaries"

            transcripts_dir.mkdir(exist_ok=True)
            summaries_dir.mkdir(exist_ok=True)

            # No audio exists on the captions path — nothing to archive or clean up.
            dest_wav = None
            if download_result.audio_path is not None:
                wav_dir = project_dir / "wav_files"
                wav_dir.mkdir(exist_ok=True)
                if keep_wav:
                    dest_wav = wav_dir / download_result.audio_path.name
                    shutil.move(str(download_result.audio_path), str(dest_wav))
                else:
                    download_result.audio_path.unlink()

            # 3b. Visual content — opt-in; the vision tier is metered spend.
            visual_markdown = ""
            if settings.extract_frames:
                logger.info("Extracting visual content from keyframes...")
                try:
                    video_file = frames_mod.download_video(source.url, project_dir / "_video")
                    if video_file:
                        keyframes = frames_mod.extract_and_dedupe(
                            video_file, project_dir / "frames"
                        )
                        # Transcript segments feed the audio-cue ranking
                        # signal ("as you can see here"); duration bounds the
                        # final frame's dwell.
                        readings = visuals.read_frames(
                            keyframes,
                            segments=transcript_result.segments,
                            video_duration=(
                                transcript_result.segments[-1].end
                                if transcript_result.segments else None
                            ),
                        )
                        visual_markdown = visuals.render_markdown(
                            readings, segments=transcript_result.segments
                        )
                        video_file.unlink(missing_ok=True)
                        shutil.rmtree(project_dir / "_video", ignore_errors=True)
                except Exception as e:  # noqa: BLE001 - enrichment is never fatal
                    logger.warning(f"Visual extraction failed: {e}")

            summary_path = summaries_dir / f"{safe_title}.summary.md"
            summary_text = visuals.insert_visual_section(
                summary_result.summary_text, visual_markdown
            )
            summary_path.write_text(summary_text, encoding="utf-8")

            # A run that produced no usable notes (silent/music-only source with
            # nothing on screen either) is a legitimate outcome, but in a
            # listing it looks identical to a real result. Mark it.
            if settings.mark_empty_results and not visuals.has_usable_notes(summary_text):
                project_dir = _mark_empty(project_dir)
                summary_path = project_dir / "summaries" / summary_path.name
                logger.warning(
                    f"[{position}] No usable notes produced — marked "
                    f"{project_dir.name}"
                )

            console.print(Panel(
                f"[bold]Processing Complete for {download_result.title}![/bold]\n\n"
                f"Output Directory: [blue]{project_dir}[/blue]\n"
                f"Summary: [blue]{summary_path}[/blue]",
                title="Success",
                border_style="green"
            ))
            done.append(source.title)

        except SubscriptionExhausted as e:
            # Policy is not "wait" (that retries in place), so end the run here
            # rather than burn the rest of the playlist on failures or spend.
            logger.error(f"[{position}] Subscription exhausted: {e}")
            stopped_at = f"{position} — {source.title}"
            break
        except KeyboardInterrupt:
            stopped_at = f"{position} — {source.title} (interrupted)"
            break
        except Exception as e:  # noqa: BLE001 - one bad video must not end the run
            logger.error(f"[{position}] Failed: {source.title} — {e}")
            failed.append((source.title, str(e)))
            continue

    # Final ledger, so an unattended run leaves a readable account of itself.
    lines = [
        f"[green]Summarized:[/green] {len(done)}",
        f"[blue]Skipped (already done):[/blue] {len(skipped)}",
        f"[red]Failed:[/red] {len(failed)}",
    ]
    for title, err in failed:
        lines.append(f"  [red]•[/red] {title[:50]} — {err[:60]}")
    if stopped_at:
        remaining = len(sources) - len(done) - len(skipped) - len(failed)
        lines.append(f"\n[yellow]Stopped at {stopped_at}[/yellow]")
        lines.append(f"[yellow]{remaining} video(s) not attempted.[/yellow]")
        lines.append("Re-run the same command to resume — finished videos are skipped.")

    console.print(Panel(
        "\n".join(lines),
        title="Run Summary" if not stopped_at else "Run Stopped",
        border_style="green" if not (failed or stopped_at) else "yellow",
    ))

    if stopped_at or failed:
        raise typer.Exit(code=1)

# Registering a second command (`book`) made typer require a subcommand, which
# would have silently broken every existing invocation — the readme, the
# overnight sweep script, and any cron entry all call
# `python -m video2mdnotes.main <url>` with no verb. Rather than change a
# published interface as a side effect of adding a feature, an unrecognised
# first argument is treated as a URL for `process`, exactly as before.
_COMMANDS = {"process", "book", "catalog", "media"}


def _with_default_command(argv: list[str]) -> list[str]:
    """Insert the implicit `process` verb when the caller omitted it."""
    if argv and argv[0] not in _COMMANDS and not argv[0].startswith("-"):
        return ["process", *argv]
    return argv


def main() -> None:
    app(args=_with_default_command(sys.argv[1:]))


if __name__ == "__main__":
    main()
