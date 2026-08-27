# Storage tiering — the Expansion drive plan

Recorded 2026-08-25. **Plan, not built.** The volume-residency machinery this
depends on *is* built (see §4); the tiering policy is not.

---

## The idea

Bulk media and data live on a large removable drive. Only what is **actively
being used** gets pulled to internal storage. The catalog stays internal and
stays complete, so everything remains searchable whether or not the drive is
attached.

Confirmed present today:

```
/Volumes/Expansion                 18 TB, ~18 TB free
/Volumes/samsung_SSD_990_pro_2tb   holds 329 catalogued books
internal                           821 GB free
```

## Why this works here specifically

Because **the index is ~100× smaller than the thing it indexes**:

```
books on disk      9.34 GB
catalog + vectors  90.5 MB      ≈ 103 : 1
```

You can search, rank, locate and plan across 61 million words with the drive
unplugged. The file is only needed at the moment something is actually read or
summarized — which is rare, deliberate, and already gated behind a cost
decision.

That ratio will be far more extreme for video. A 4 GB `.MOV` produces a
transcript of a few tens of kilobytes.

## Tiers

| Tier | Lives | Holds | Needed when |
|---|---|---|---|
| **0 — index** | internal, always | catalog, vectors, transcripts, notes | always |
| **1 — working set** | internal | whatever a current lesson touches | while learning |
| **2 — bulk** | Expansion (18 TB) | video, audio masters, full book corpus | only on read |
| **3 — cold** | Expansion, archived | superseded runs, raw downloads | rarely |

**Tier 0 is the load-bearing one.** As long as it is internal and complete, an
unplugged drive degrades the system from "read this book" to "tell me which
book" — a real loss, but not a broken system.

## What must be true before this is safe

1. ~~Catalog records which volume each file is on~~ — **done**, `books.volume`.
2. ~~`present` / `offline` / `missing` are distinguished~~ — **done**.
   OFFLINE must never be reported as an error, or the report becomes noise and
   stops being read.
3. ~~Moved files can be found again~~ — **done**, `catalog relocate`.
4. **Promotion/demotion is not built.** Moving a file between tiers must update
   the catalog atomically, or the next search points at a path that no longer
   exists. Today a copy is followed by a manual re-index.
5. **Transcripts and notes must never live only on tier 2.** They are the
   expensive derived artifacts. Losing the source video is an inconvenience;
   losing the transcript means paying to make it again.

## Open questions

- **Promotion trigger.** Explicit (`corpus fetch <title>`) or implicit (pull on
  first read)? Implicit is friendlier and hides a multi-second copy behind what
  looks like a local operation.
- **Eviction.** Never? LRU? Manual? Doing nothing is fine for a long time given
  821 GB free.
- **Two drives.** `Expansion` (18 TB, spinning?) and `samsung_SSD_990_pro_2tb`
  have very different performance. Books are small and random-access — SSD.
  Video is large and sequential — Expansion.
- **Backup.** Tiering is not backup. A single 18 TB drive holding the only copy
  of the media is a single point of failure, and this plan does not address it.

## The failure this design is really about

> "Humans and agents get messy and don't put away their toys."

Files get moved, copied, renamed and left in `~/Downloads`. A catalog of
absolute paths rots quietly, and the failure looks like a search result that
points at nothing — a wrong answer wearing the costume of a right one.

Hence: verification is **free** (`Path.exists()` per row, milliseconds for 715
books) so it can run constantly, and relocation is **conservative** (name *and*
byte size must match) so recovery never silently attaches the wrong edition.

## Commands that exist today

```
catalog volumes            which volumes, how many books, mounted?
catalog verify             present / offline / missing — free, run often
catalog relocate           dry run: find moved books elsewhere
catalog relocate --force   apply
catalog relocate --corpus <dir>   search a specific root
```

## Recommended next

1. **Run `catalog verify` on a schedule** — it is free and it is the early
   warning for everything above.
2. **Decide the promotion trigger** before moving bulk media to Expansion.
   Copying 5 TB and then discovering the ergonomics are wrong is expensive.
3. **Keep tier 0 backed up** independently of the tiering scheme.
