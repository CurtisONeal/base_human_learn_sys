from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pathlib import Path

# Project root directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent

PROMPT_VARIANTS = ("current", "legacy")


class Settings(BaseSettings):
    """
    Application settings loaded from .env file and environment variables.
    """
    # LLM Provider Configuration (summarization only — transcription is local)
    #
    # llm_models is an ordered FAILOVER chain, not a quality ladder: entry 1 does
    # essentially all the work, and later entries fire only when an earlier one
    # *errors*. Putting a cheap model first makes it your summarizer.
    #
    # Three backend kinds are understood, by prefix:
    #   "claude-cli/<alias>"  -> subprocess to the `claude` CLI, authenticated
    #                            with the logged-in subscription (no API key, no
    #                            metered billing). Aliases: opus/sonnet/haiku.
    #   "openai/…", "anthropic/…"
    #                         -> metered REST via litellm; needs the matching key.
    #   "ollama/…", "lm_studio/…", …
    #                         -> local/self-hosted via litellm; needs NO key, so
    #                            it is gated behind allow_local_models (below).
    #   "nous/<model>"        -> Nous inference API (OpenAI-compatible) at
    #                            nous_api_base with nous_api_key. Metered.
    #   "hermes/<model>"      -> local `hermes proxy` (OpenAI-compatible, OAuth
    #                            credential attached by the proxy). Treated as
    #                            metered and billed to hermes_proxy_provider.
    # Every metered call is checked against the cost ledger caps first.
    llm_models: list[str] = [
        "claude-cli/opus",     # subscription, deepest analysis
        "claude-cli/sonnet",   # subscription, faster fallback
        "openai/gpt-4o",       # metered — only if the subscription is unavailable
    ]

    # Local models need no API key. Without this gate a misconfigured chain could
    # silently fall through to a much weaker model and emit notes that look fine,
    # so enabling local summarization must be an explicit, deliberate choice.
    allow_local_models: bool = False

    # What to do when the subscription backend runs out of capacity mid-run.
    #   "wait"    -> alert, sleep until the limit resets, retry the same video.
    #                Default, because an unattended run (e.g. a research agent
    #                that cannot answer a prompt) must NOT start spending money
    #                just because nobody was there to say no. It waits for the
    #                capacity that is already paid for.
    #   "metered" -> continue on the paid backends in the chain.
    #   "local"   -> continue, but only on local backends (needs
    #                ALLOW_LOCAL_MODELS and a local entry in LLM_MODELS).
    #   "stop"    -> stop cleanly; finished videos are kept and skipped on resume.
    #   "ask"     -> prompt once, then apply that answer for the rest of the run.
    on_exhaustion: str = "wait"
    # Used only when the CLI does not report a reset time in its error output.
    exhaustion_wait_seconds: int = 5 * 60 * 60
    # Upper bound, so a misparsed reset time cannot park a run for days.
    exhaustion_max_wait_seconds: int = 12 * 60 * 60

    # `claude` CLI used by the claude-cli/ backend. Resolved on PATH if left bare.
    claude_cli_path: str = "claude"
    # `claude -p` is an AGENT, not a completion endpoint: by default it can load
    # skills, search the web, and read files. Transcripts are untrusted
    # third-party content, so tools are disabled — see README.md.
    claude_cli_disallowed_tools: str = (
        "Skill,WebSearch,WebFetch,Read,Bash,Glob,Grep,Task,Agent,Edit,Write"
    )
    claude_cli_timeout: int = 900
    # annotate-people web lookup: one call searches/fetches for up to 10 targets.
    claude_cli_web_timeout: int = 1200

    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    # anthropic/* routes (LLM_MODELS entries and the vision tier) are refused
    # unless this is true: the Anthropic API credit is reserved for the advisor
    # and code review, and the pipeline must never spend it.
    allow_anthropic_api: bool = False

    # Escalate to METERED backends when the subscription is exhausted, without
    # changing ON_EXHAUSTION. False keeps the policy above exactly as it is
    # (default "wait": no paid call). True lets an exhausted run continue on the
    # paid entries in LLM_MODELS, still bounded by the spend caps below; when
    # those are spent too, SubscriptionExhausted is raised so the run waits
    # (or stops) per ON_EXHAUSTION.
    allow_metered_fallback: bool = False

    # Hard spend caps, enforced by core/costs.py BEFORE each metered call using
    # a worst-case estimate (prompt tokens + METERED_MAX_OUTPUT_TOKENS).
    # METERED_USD_CAP covers all metered providers combined; PROVIDER_USD_CAPS
    # adds optional per-provider caps, keyed by ledger provider name, e.g.
    #   PROVIDER_USD_CAPS='{"openai": 10, "nous": 5}'
    # A capped provider is skipped and the chain moves to the next one; when all
    # are capped the run stops with BudgetExhausted. claude-cli/ (subscription)
    # calls are recorded at $0 and are never blocked.
    metered_usd_cap: float = 10.0
    provider_usd_caps: dict[str, float] = {}
    # PER-RUN cap, on top of the cumulative ones above: metered spend plus open
    # reservations recorded under THIS run's run_id only (one process, or
    # RUN_ID when set). Unset/empty = no per-run cap. 0 = no metered call at
    # all in this run (and the vision tier is skipped, since it is metered and
    # not ledgered). The CLI flag `--usd-cap` (process/media/book/longform) and
    # the API's `usd_cap` field override it for one invocation.
    run_usd_cap: float | None = None
    # Passed as max_tokens on metered calls, so the estimate is a real bound.
    metered_max_output_tokens: int = 16000
    # Persistent ledger shared by every run pointed at it. Delete or re-point
    # to start a fresh budget.
    cost_ledger_path: Path = BASE_DIR / "previous_run_results" / "_cost_ledger.json"
    # Optional run id recorded with each ledger entry (default: per process).
    run_id: str = ""

    # Nous inference API (OpenAI-compatible). Chain entries "nous/<model>",
    # e.g. "nous/Hermes-4-405B", are sent to this base with NOUS_API_KEY.
    nous_api_base: str = "https://inference-api.nousresearch.com/v1"
    nous_api_key: str | None = None
    # Local Hermes subscription proxy (`hermes proxy start`), which forwards
    # OpenAI-compatible requests to an OAuth provider and attaches the real
    # credential itself — any bearer is accepted. Entries "hermes/<model>",
    # e.g. "hermes/Hermes-4-70B". The proxy must be started separately.
    hermes_proxy_base: str = "http://127.0.0.1:8645/v1"
    # Ledger/cap name for hermes/ entries: the upstream the proxy bills, so a
    # PROVIDER_USD_CAPS "nous" cap covers Nous direct and Nous-via-proxy alike.
    hermes_proxy_provider: str = "nous"

    # Deprecated: retained for backward-compat with existing .env / docker-compose
    # (LLM_MODE / LLM_PROVIDER / LLM_MODEL, openai_model / anthropic_model).
    # The summarizer now uses the llm_models chain above.
    llm_mode: str = "openai"
    openai_model: str = "gpt-4o-mini"
    anthropic_model: str = "claude-haiku-4-5"
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"

    # Captions-first Configuration
    #
    # When a source ships a human-authored transcript, using it skips both the
    # audio download and the ~10-minute Whisper run. Only manual tracks are ever
    # used — machine `automatic_captions` are ASR without our vocabulary hint,
    # so Whisper is the better fallback. See core/captions.py.
    captions_first: bool = True
    # A caption track must yield at least this many word-equivalents, or it is
    # treated as unusable (music videos return cues that are entirely "[♪♪♪]").
    # CJK text is counted by character, since it is not space-delimited.
    captions_min_words: int = 20
    # Ordered, comma-separated language preference for caption tracks, e.g.
    # "en,ja" or "ja,any". The token "any" accepts whatever manual track the
    # source ships. Empty falls back to FW_LANG. Kept separate from FW_LANG
    # because that setting tells Whisper what to expect, whereas a source in
    # another language can still be worth taking captions from.
    captions_lang: str = ""

    # When yt-dlp cannot handle a URL at all, fetch the page once and probe any
    # embedded video URLs found in it. Covers course pages and blogs whose
    # player is injected by JavaScript. Never logs in, runs no JavaScript, and
    # defeats no access control — an anonymous fetch either exposes embeds or
    # yields nothing.
    scrape_page_embeds: bool = True

    # When the source URL is a playlist, put every video's notes inside one
    # directory named after the playlist, instead of scattering them by date
    # across the output root. Off by default so existing layouts don't move.
    group_by_playlist: bool = False

    # Prefix directories whose run produced no usable notes with "no_summary_".
    # A silent/music-only video yields a placeholder summary that looks like a
    # real result in a listing; the prefix makes the gap visible at a glance.
    mark_empty_results: bool = True

    # Content-shape profiles for visual extraction. See core/profiles.py.
    # Precedence: --profile > PROFILE_SOURCE_MAP > probe > these globals.
    profile_source_map: str = (
        "3blue1brown=animated,postman=screencast,karpathy=lecture,"
        "freecodecamp=screencast,khan=lecture"
    )
    # Probe: classify by OCR yield on already-extracted frames. Free, and only
    # ever changes what gets PAID for — never re-extracts to act on a guess.
    profile_probe: bool = True
    profile_probe_min_samples: int = 3
    profile_probe_dense_words: int = 60   # >= this median -> screencast/UI
    profile_probe_sparse_words: int = 20  # >= this median -> lecture

    # Subject domains (summarizer prompt selection). See core/domains.py.
    # YAML in configuration, a dict in code — add a domain without touching
    # Python, and everything downstream still gets a typed, testable object.
    domain_library: str = ""
    domain_source_map: str = ""
    default_domain: str = "generic"
    domain_probe: bool = True
    domain_probe_min_distinct_terms: int = 3
    domain_probe_min_rate: float = 1.0
    domain_probe_margin: float = 2.0
    # None means "resolve from the domain"; an explicit PROMPT_FILE still wins.
    prompt_file_override: Path | None = None

    # Visual Content Extraction (off by default — the VLM tier costs money)
    #
    # Some instructional video carries its substance on screen, not in the
    # audio. This samples keyframes at scene changes, reads them with local OCR,
    # and escalates only the frames OCR cannot explain to a vision model.
    extract_frames: bool = False
    # ffmpeg scene-change sensitivity (0-1). Lower catches more transitions.
    # 0.15 measured 2026-08-16: 0.3 is tuned for hard slide cuts and yielded
    # only 2 frames from a 19-minute animated explainer, while 0.15 yielded 60
    # raw / 24 after de-duplication. Continuous animation has no hard cuts, so
    # err low and let the perceptual-hash dedupe absorb the over-firing.
    frame_scene_threshold: float = 0.15
    frame_max: int = 40
    # When scene detection yields fewer than this many frames, fall back to
    # interval sampling. Continuously-animated video has no hard cuts, so scene
    # detection can miss content that is plainly on screen.
    frame_min_before_interval: int = 4
    # Roughly how many frames interval sampling should aim for, so short clips
    # and long lectures both get a sensible cadence.
    frame_interval_target: int = 10
    # Perceptual-hash distance below which two frames count as the same slide.
    # Screencasts over-fire scene detection on cursor movement and typing.
    frame_dedupe_threshold: int = 6
    # Minimum seconds between kept keyframes. A cut or flash can emit several
    # frames inside a fraction of a second; perceptual hashing keeps them
    # because they genuinely differ, so the gap is enforced in time instead.
    frame_min_gap_seconds: float = 1.0
    # Frames with less greyscale contrast than this are treated as blank
    # transition frames and never sent to the vision model — it confabulates on
    # featureless input rather than declining. Measured separation: a
    # hallucinated flash frame scored 21.9, real content scored 38.2+.
    frame_blank_min_contrast: float = 30.0
    # A frame whose OCR overlaps the transcript this much is treated as
    # burned-in subtitles rather than on-screen content.
    subtitle_overlap: float = 0.6
    subtitle_min_words: int = 3
    frame_video_height: int = 720
    frame_extract_timeout: int = 600
    ffmpeg_path: str = "ffmpeg"

    # OCR runs on every frame: free and local, so there is no reason not to.
    #   "auto" -> macOS Vision framework when available, otherwise skip
    #   "none" -> disable OCR (every frame escalates to the vision model)
    # NOTE: local OCR is macOS-only. On Linux/Windows this degrades to
    # vision-model-only, which is metered spend — see README.md.
    ocr_backend: str = "auto"
    ocr_min_confidence: float = 0.4

    # Vision-model tier. This CANNOT use the claude-cli subscription backend:
    # that runs with Read disabled as the prompt-injection guard, and images
    # cannot be passed without reopening it. Vision is metered API spend.
    vlm_enabled: bool = True
    # Opus for depth, Haiku for ~15x lower cost. Roughly $0.72 vs $0.05 per
    # video at ~30 frames; both halve again via the Batch API.
    vlm_model: str = "anthropic/claude-opus-4-8"
    # Frames whose OCR yields fewer real words than this are treated as
    # picture-carrying (charts, diagrams, UI) and escalated.
    vlm_escalate_below_words: int = 12
    # Hard ceiling on escalations per video, so one dense deck cannot run away
    # with the bill. Which frames get spent on is decided by core/ranking.py,
    # not by chronological order — otherwise the budget is exhausted on the
    # intro and the conclusion is never seen.
    vlm_max_frames: int = 15
    # Seconds either side of a frame whose narration is quoted beneath it, so a
    # reader can connect what was on screen to what was being said.
    visual_narration_window: float = 4.0

    # Ranking weights for choosing WHICH eligible frames to spend the budget on.
    # All three signals are free — computed from data already on hand.
    # Dwell dominates by design: how long a frame stayed on screen is the
    # strongest available proxy for "this was deliberate content".
    rank_weight_dwell: float = 0.45
    rank_weight_uniqueness: float = 0.30
    rank_weight_audio_cue: float = 0.25
    # Phrases that mean "the speaker is pointing at the screen". Only usable
    # when there is narration, which is why this ranks rather than triggers.
    rank_cue_phrases: str = (
        "as you can see,look at,here we have,this shows,notice that,"
        "on the screen,this diagram,this chart,this table,shown here,"
        "take a look,over here,in this figure,notice how"
    )
    # Seconds either side of a frame in which a cue phrase counts as related.
    rank_cue_window: float = 8.0
    # The last keyframe's dwell is capped at this multiple of the LONGEST gap
    # actually observed. Without it, a run truncated by FRAME_MAX credits its
    # final frame with every remaining second of the video, and that outlier
    # flattens every other frame's score. Multiple of the longest rather than
    # the median because transition-heavy content has a ~1s median, which would
    # crush a genuinely held closing slide.
    rank_final_dwell_clamp: float = 1.0

    # faster-whisper Configuration
    # Measured on a 14-minute far-field recording (Downloads/20260820_191152.MOV,
    # people talking across a room — close to worst case):
    #
    #   medium           63s   318 words   0 repeated 5-grams
    #   large-v3        352s   473 words  10 repeated 5-grams  <- looping
    #   distil-large-v3  78s   635 words   0 repeated 5-grams  <- best
    #
    # distil-large-v3 keeps large-v3's full 32-layer encoder (where acoustic
    # robustness lives) and cuts the decoder to 2 layers, so it is nearly as
    # fast as medium at the same ~1.5 GB while recovering twice the speech.
    #
    # distil-large-v3 IS ENGLISH-ONLY, so it cannot be the default: this corpus
    # contains Aikido material and Japanese terminology, and an English-only
    # model turns that into confident phonetic nonsense.
    #
    # The default is therefore multilingual, assuming close-mic speech.
    # core/asr_select.py switches to distil-large-v3 automatically when the
    # audio measures far-field AND no non-English signal is present. Setting
    # FW_MODEL explicitly pins the model and disables that selection.
    fw_model: str = "large-v3"

    # Auto-select between large-v3 and distil-large-v3 from free signals
    # (metadata vocabulary + one ffmpeg loudness pass). Off = always fw_model.
    asr_auto_select: bool = True
    # Mean programme level below which audio counts as far-field/noisy.
    # Calibrated on ONE real sample (-33.0 dB); a starting point, not a tuned value.
    asr_noisy_mean_db: float = -28.0
    fw_compute: str = "auto" # auto, int8, float16, int8_float16
    fw_lang: str = "en"

    # Windowed ASR (core/longasr.py). EVERY local recording and every
    # caption-less download is transcribed in bounded windows (a short clip is
    # one window) — never converted whole to one wav. There is no length
    # threshold any more (WP-F9): the rule is structural, not conditional.
    #   ASR_BACKEND: "auto" (parakeet-mlx when available and the material is
    #   English; else faster-whisper:distil-large-v3; non-English context always
    #   faster-whisper:large-v3), or an explicit "name[:model]".
    asr_backend: str = "auto"
    asr_window_s: float = 1500.0
    asr_overlap_s: float = 8.0
    # Completeness gate: longest allowed un-transcribed stretch that is not
    # detected silence, and the minimum audio coverage of the summed duration.
    asr_max_gap_s: float = 120.0
    asr_min_coverage: float = 0.98
    # Window silence detection (ffmpeg silencedetect) — what counts as silent.
    asr_silence_db: float = -40.0
    # Segment-length cap (core/asr/segmenting.py), every backend: a segment
    # longer than either is split at sentence/clause breaks using the engine's
    # word timings. The transcript stores start times only, so a long segment
    # is both a citation-precision loss and a false "gap" to the evaluator.
    asr_max_segment_s: float = 20.0
    asr_max_segment_words: int = 60

    # File Paths (defaults are relative to project root)
    output_dir: Path = BASE_DIR / "previous_run_results"
    # Where every command records its last run (WP-M3); None = output_dir/_last_run.json.
    last_run_file: Path | None = None
    # Learning packages (core/package.py, `package` / `--package <subject>`):
    # <obsidian_vault>/<learning_root>/<subject>/<slug>/ holds notes + text +
    # images; source binaries go to <obsidian_vault>/__sources/<subject>/<slug>/.
    obsidian_vault: Path = Path("/Users/s_admin/Documents/obsidian_vault/syncing_vault2_clean_2026")
    learning_root: str = "010_learning_system/by_subject"
    temp_dir: Path = BASE_DIR / "temp_files"
    prompt_file: Path = BASE_DIR / "summarize_prompt.txt"
    # Summary prompt version, so two prompt generations can be A/B tested on
    # the same transcript (CLI: --prompt-variant). "current" adds the People
    # section (who is who: role, stated bio, relevance, citations) to the
    # long-form passage prompt and the document prompts; "legacy" is exactly
    # the prompts as they were before it (main @ 7fcd1d7). Recorded in the
    # notes' frontmatter (`prompt_variant`) and in longform_state.json; a
    # passage checkpointed under another variant is re-run, never reused.
    prompt_variant: str = "current"

    @field_validator("run_usd_cap", mode="before")
    @classmethod
    def _blank_cap_is_none(cls, value):
        # RUN_USD_CAP= (empty, as .env.example ships it) means "no per-run cap".
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("prompt_variant", mode="before")
    @classmethod
    def _known_variant(cls, value):
        v = (value or "current").strip().lower() if isinstance(value, str) else value
        if v not in PROMPT_VARIANTS:
            raise ValueError(f"PROMPT_VARIANT must be one of {', '.join(PROMPT_VARIANTS)}")
        return v

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding='utf-8',
        extra='ignore'
    )

# Instantiate settings
settings = Settings()

# Auto-detect compute type if set to 'auto'.
# faster-whisper (ctranslate2) has no CUDA backend on macOS, so it runs on the
# CPU for BOTH Intel and Apple Silicon Macs. CPU float16 is unsupported by
# ctranslate2 and raises at model load, so int8 is the correct default here.
# (Reserve float16 for a real CUDA GPU by setting FW_COMPUTE=float16 explicitly.)
if settings.fw_compute == "auto":
    settings.fw_compute = "int8"
