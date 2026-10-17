# MVP integration fixtures (WP-M1b)

Small inputs for `tests/test_integration_mvp.py` and `scripts/smoke_all_paths.sh`.
About 235 KB in total. Regenerate everything with:

    .venv/bin/python tests/fixtures/mvp/make_fixtures.py            # or --only docs,captions,audio

The audio is synthetic speech from macOS `say` (voice Samantha, rate 175) encoded
with ffmpeg. No user recording is used. The script never reads `/Volumes`, the CLE-AI
safety copies or the iMovie library. It only *reads* the CCA-F captions run in
`previous_run_results/`.

| File | Bytes | How it is made | Used by |
|---|---:|---|---|
| `guide.md` | 698 | written by the script: H1 + 3 H2 sections | `process` local md |
| `notes.txt` | 356 | written by the script: plain paragraphs | `process` local txt |
| `article.html` | 667 | written by the script: h1 + 2 h2 | `process` local html; body served for the article URL |
| `embed_page.html` | 296 | written by the script: page with a YouTube `<iframe>` embed (`mvpEmbed001`) | `process` embedded-video page |
| `tiny_book.epub` | 2,161 | EPUB 3 zip written by hand (OPF, nav, 3 chapters, one `<pre><code>`) | `process` epub, `book`, `book --extract-only`, `catalog` |
| `tiny_paper.pdf` | 2,840 | pymupdf: 3 pages, 3 bookmarks, metadata title "Chunk Size and Retrieval" | `process` pdf, `book`, `catalog` (and the PDF-title regression) |
| `cca_slice.en.json3` | 75,033 | first 7 min (0–420 s) of the CCA-F auto-caption json3, with only the `tStartMs/dDurationMs/wWinId/aAppend/segs[utf8,tOffsetMs]` keys kept | captions path (`process`, playlist, API). Its real "cloud code" mishearings give `corrections.json` 7 entries |
| `yt_info.json` | 1,806 | CCA-F `yt_info.json` cut down to the metadata keys. Duration 420, auto captions only, no chapters | base for the ASR-video variants (built in the test) |
| `yt_info_platform_chapters.json` | 2,135 | as above, plus 4 platform `chapters` fitted to the slice | `process` with captions (rung `platform`), playlist |
| `yt_info_description_chapters.json` | 1,897 | as above, but the 4 chapters are a description timestamp list | `process` with captions (rung `description`), API |
| `talk_15s.m4a` | 45,671 | `say` → AAC/MP4, mono 16 kHz, padded to 15 s | `media` single file, `--transcribe-only` → `longform`, ASR-video download (`process` with no captions, playlist, embed, API) |
| `timkoo_truncated.aac` | 26,871 | `say` → ADTS AAC, then the last 10 % of bytes cut at an odd offset (the final frame is incomplete). ffmpeg reports `Input buffer exhausted`, the duration is a bitrate estimate (6.43 s) and the last words are lost | `media` with a truncated recorder file |
| `cam_0001.mov`, `cam_0002.mov` | 25,908 / 28,749 | 10 s each: black 160x120 h264 at 5 fps, `say` speech as AAC, `creation_time` 22:00:00Z and 22:00:10Z (contiguous) | `media` camera-split auto-group, `--probe` |
| `speech_scripts.json` | 601 | the words spoken in each audio file | the scripted fake ASR (default suite), the recall check (real parakeet) |
| `make_fixtures.py` | — | the generator | — |

Chapters fitted to the slice (`Welcome` 0:00, `Introduction` 0:47 (the real one),
`Study plan and costs` 2:54, `Content outline` 5:49) were picked from what the speaker
says at those times. They are **not** the video's published chapters. The real
description lists 13 chapters, and 12 of them start after 7:00.

`.gitignore` ignores `*.m4a`/`*.aac` repo-wide. This directory is excepted
explicitly, so the fixtures get committed.
