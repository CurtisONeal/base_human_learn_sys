---
note_type: reference
title: "Timkoo recorder, local audio, and the transfer problem"
tags: [from-octobob, claude, audio, timkoo, video2notes]
updated: 2026-08-25
---

# Timkoo, local audio, and getting data onto the Mac

Written 2026-08-25 after building WP-03 (local media input).

## The short version

Two different problems get confused with each other:

| Problem | Who solves it |
|---|---|
| **Decoding** the recorder's file format | ✅ solved — ffmpeg handles all of them |
| **Getting the file off the device** | ❌ not solvable in code; depends on the device |

Everything below separates the two, because "the tool can't read my recording"
is usually the second problem wearing the first one's clothes.

## Decoding: solved

`ffmpeg` on octobobs-mini carries decoders for every format a consumer voice
recorder emits. Verified, not assumed:

```
mp3  aac  amr_nb  amr_wb  wmav2  opus  vorbis  flac
adpcm_ima_wav  gsm  speex  sbc  pcm_*
```

**The important trap: extension is not format.** Cheap recorders routinely
write `.wav` files containing ADPCM or GSM rather than PCM, and `.mp3` files
that are really MPEG-2.5 at 8 kHz. The tool probes each file and reports what is
actually inside:

```
media ~/Recordings --probe        # free. no transcription, no model.

  adpcm_8k.wav   adpcm_ima_wav · 8000 Hz · mono · looks like a voice recording
  recorder.wma   wmav2 · 8000 Hz · mono · looks like a voice recording
  screen.mp4     aac · 44100 Hz · video+audio
  silent.mp4     NoAudioStreamError — nothing to transcribe
  notmedia.mp3   UnreadableMediaError — may be a proprietary container
```

Anything mono at ≤24 kHz gets flagged *looks like a voice recording*, because
that shape is what recorders produce and music never is. It gates nothing; it
just means a surprising transcript has an explanation.

## Transfer: three cases

This is the part that has been a fight. Identify which case you are in first:

| Case | Symptom | What to do |
|---|---|---|
| **USB mass storage** | device appears in `/Volumes` | `media /Volumes/NAME --probe` — **point straight at it, no copying** |
| **MTP** | plugs in, nothing mounts, no error | macOS cannot mount MTP. Needs Android File Transfer or similar. Nothing in the repo helps |
| **Proprietary container** | files copy fine, won't decode | export to WAV/MP3 from the vendor's own software first |

The third case is where people give up, so the tool names it explicitly rather
than reporting a generic decode failure.

**Not yet tested against the actual Timkoo** — it was not connected when this
was built. Plug it in and run `media /Volumes/<name> --probe`. That is free and
tells us which case we are in.

## What was actually in ~/Downloads (2026-08-25)

Checked for the "two attempts at digitising a cassette":

- **No cassette audio found.** The two `device-audio-tape*.svg` files are
  byte-identical SVG *icons* (same MD5), not recordings.
- `20260820_191152.MOV` — 13:58, aac 48 kHz stereo, 4.2 GB. Real. Transcribed.
- `Accelerate Beat.caf` — 7 s, an Apple loop from 2015.
- `data-U8FS-wNn.d.mts` — a **TypeScript** declaration file. It was being picked
  up as MPEG transport stream; `.d.mts`/`.d.ts` are now excluded. Fixed.

If the cassette transfers exist, they are somewhere else.

## What the real recording showed

The `.MOV` is the first genuine end-to-end test: 14 minutes of far-field
ambient audio (people examining a camera, then what sounds like martial arts
instruction).

**427 words from 14 minutes.** The summarizer's verdict:

> "Insufficient transcript content to determine. The audio transcript is
> severely garbled and fragmented... too incoherent to summarize accurately."

**That is the correct behaviour**, and the most important result here. It
noticed the shift to martial-arts content around the 6-minute mark and still
declined to invent instruction sequences. A system that had produced a
confident, plausible summary of that audio would be worse than useless.

### The limitation it exposes

Far-field ambient audio transcribes poorly at the current setting:

```
fw_model = "medium"      (1.5 GB)
```

octobobs-mini is an M4 with 32 GB. Two upgrades are affordable:

| Model | Size | Trade |
|---|---|---|
| `large-v3` | 3.1 GB | best accuracy, slowest |
| `distil-large-v3` | 1.5 GB | near-large accuracy, ~6x faster |

**`distil-large-v3` is probably the right default for recorder audio** — same
memory as `medium`, materially better on noisy far-field input. Worth trying
against this same `.MOV`, which now serves as a known-hard benchmark.

A close-mic Timkoo recording should do considerably better than this file
regardless — a dojo at 3 metres is close to the worst case.

## Commands

```
media ~/Downloads --probe              # what have I got? free
media /Volumes/RECORDER --probe        # straight off the device
media ~/Recordings/meeting.m4a         # transcribe + summarize
media ~/Recordings --keep-wav          # keep the 16kHz conversion
```

Same artifact layout as a YouTube video: `<slug>/transcripts/` and
`<slug>/summaries/`. The transcript is written **before** summarizing, so a
summarizer failure never costs the transcription.
