"""Subject-domain selection for the summarizer.

Separate from `core/profiles.py` on purpose. A *visual profile* describes what a
video looks like (screencast, animated, lecture); a *domain* describes what it
is about (technical, aikido, boardgame). They correlate but are not the same
axis: OCR yield distinguishes a screencast from a diagram explainer, but cannot
tell an Aikido demonstration from a dance tutorial — both are "no text, motion".

This does not replace ADR-035's domain-adaptive prompt; it makes self-detection
the *default* rather than the *only* mechanism. `generic` is that prompt,
byte-identical, and remains the honest answer for material nothing knows
anything about. Selection supersedes it only where there is evidence.

Two free signals, in the order they become available:

* **Metadata vocabulary** — yt-dlp tags and description, already used by
  `build_initial_prompt()` for Whisper's hint. Available *before* download, so
  it can pick a prompt without spending anything.
* **Transcript words** — available after transcription, and used to confirm or
  correct the metadata guess.

Nothing here touches extraction, OCR, escalation or the vision budget. A
text-derived guess must not gain authority over frame spend (ADR-040).
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic import BaseModel

from video2mdnotes.config import BASE_DIR, settings
from video2mdnotes.logger import logger

PROMPT_DIR = BASE_DIR / "prompts" / "summarize"


class DomainEvidence(BaseModel):
    """What an evaluation run learned about a domain. Filled in later."""
    videos: int
    none_section_rate: float
    measured_on: str


class Domain(BaseModel):
    name: str
    why: str
    prompt_path: Path
    # Empty means never auto-detected — correct for `generic`, which is the
    # fallback rather than something to match against.
    detect_terms: List[str] = []
    evidence: Optional[DomainEvidence] = None


DOMAINS: Dict[str, Domain] = {
    "generic": Domain(
        name="generic",
        why="ADR-035's domain-adaptive prompt; validated on technical and Aikido content",
        prompt_path=PROMPT_DIR / "generic.md",
    ),
}


def load_domains(raw_yaml: str) -> None:
    """Merge domain definitions supplied as YAML into the in-memory registry.

    YAML in configuration, a dict in code: operators add a domain without
    touching Python, and everything downstream still gets a typed object it can
    be tested against.

        DOMAIN_LIBRARY: |
          aikido:
            why: "reasoned, not measured"
            prompt: aikido.md
            detect_terms: [uke, nage, tenkan, dojo, shihonage]

    A domain whose prompt file is missing is skipped with a warning rather than
    raising: a typo in configuration should not take down a run that would
    otherwise summarize fine with `generic`.
    """
    if not (raw_yaml or "").strip():
        return
    try:
        parsed = yaml.safe_load(raw_yaml) or {}
    except yaml.YAMLError as e:
        logger.warning(f"DOMAIN_LIBRARY is not valid YAML, ignoring: {e}")
        return

    for name, spec in (parsed.items() if isinstance(parsed, dict) else []):
        spec = spec or {}
        prompt = PROMPT_DIR / str(spec.get("prompt", f"{name}.md"))
        if not prompt.exists():
            logger.warning(f"Domain '{name}' skipped — no prompt at {prompt}")
            continue
        DOMAINS[name] = Domain(
            name=name,
            why=str(spec.get("why", "reasoned, not measured")),
            prompt_path=prompt,
            detect_terms=[str(t).lower() for t in (spec.get("detect_terms") or [])],
        )


def _rate_per_1000(text: str, terms: List[str]) -> tuple:
    """(distinct terms matched, hits per 1000 words) for one domain."""
    if not terms or not text:
        return 0, 0.0
    words = re.findall(r"[^\W\d_]+", text.lower())
    if not words:
        return 0, 0.0
    joined = " ".join(words)
    hits = distinct = 0
    for term in terms:
        n = joined.count(term.lower())
        if n:
            distinct += 1
            hits += n
    return distinct, hits * 1000.0 / len(words)


def classify_by_text(text: str) -> Optional[str]:
    """Infer a domain from vocabulary. Returns None unless confident.

    Three conditions must all hold, because a wrong domain silently swaps the
    summarizer prompt: enough *distinct* terms (not one word repeated — that is
    what stops a video *about* Japanese vocabulary reading as aikido), a
    high enough rate, and a clear margin over the runner-up.
    """
    scored = []
    for domain in DOMAINS.values():
        if not domain.detect_terms:
            continue
        distinct, rate = _rate_per_1000(text, domain.detect_terms)
        if distinct >= settings.domain_probe_min_distinct_terms and rate >= settings.domain_probe_min_rate:
            scored.append((rate, domain.name))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] < scored[1][0] * settings.domain_probe_margin:
        return None
    return scored[0][1]


def resolve(source=None, transcript_text: str = "", explicit: Optional[str] = None) -> Domain:
    """Pick a domain: explicit > source map > text probe > default."""
    if explicit:
        name = explicit.strip().lower()
        if name not in DOMAINS:
            raise ValueError(
                f"Unknown domain {name!r}. Known: {', '.join(sorted(DOMAINS))}"
            )
        logger.info(f"Domain '{name}' (explicit)")
        return DOMAINS[name]

    if source is not None:
        from video2mdnotes.core.profiles import parse_source_map
        haystack = " ".join(
            str(getattr(source, f, "") or "")
            for f in ("uploader", "channel", "url", "playlist_title", "title")
        ).lower()
        for needle, name in parse_source_map(settings.domain_source_map, DOMAINS):
            if needle in haystack:
                logger.info(f"Domain '{name}' (source map)")
                return DOMAINS[name]

    # Metadata vocabulary first (free, available before download), then the
    # transcript, which can correct it.
    if settings.domain_probe:
        metadata_text = " ".join(
            str(getattr(source, f, "") or "") for f in ("title", "description")
        ) if source is not None else ""
        for label, text in (("metadata", metadata_text), ("transcript", transcript_text)):
            guess = classify_by_text(text)
            if guess:
                logger.info(f"Domain '{guess}' ({label} vocabulary)")
                return DOMAINS[guess]

    return DOMAINS[settings.default_domain]


# --- Long-form section profiles (core/longform.py) ---------------------------
#
# A long recording is summarized one passage at a time, and every passage must
# use the SAME fixed sections so assembly can pull the cross-cutting ones
# (uncertainty, time-sensitive claims, fact-checks) out and merge them. The
# single-call `generic` prompt above is not used there; these lists are.
#
# `exam-prep` adds an exam section and is chosen only on metadata evidence
# (title/description/tags naming an exam or certification) — a free signal,
# like the rest of this module.

LONGFORM_SECTIONS: Dict[str, List[tuple]] = {
    "course": [
        ("Summary",
         "6-15 bullets: the main ideas and steps of this passage, in order, each cited."),
        ("Key Concepts & Vocabulary",
         "`term` - one-line definition, cited where it is introduced."),
        ("Commands / Code / Config",
         "Commands, code, file names, settings and config keys exactly as stated "
         "(code in ``` fences), each with one line saying what it does. Cited."),
        ("Special Attention",
         "Subtle, easy-to-get-wrong or explicitly warned-about points "
         "(gotchas, security, common mistakes). Cited. A computation or "
         "correction of your own is labelled `Check:` here too."),
        ("Transcription Uncertainty",
         "The table described under Transcription Uncertainty below."),
        ("Time-Sensitive Claims",
         "The table described under Time-sensitive claims below."),
        ("Fact-Checked Notes",
         "Claims you can check against your own knowledge: correct / outdated / "
         "wrong as of the published date, with the citation. `- None` if nothing."),
    ],
}
LONGFORM_SECTIONS["exam-prep"] = (
    LONGFORM_SECTIONS["course"][:3]
    + [("Exam-relevant points",
        "What a candidate must know for the exam from this passage: definitions, "
        "distinctions, numbers, 'which option is best' reasoning, and anything the "
        "speaker says is tested or commonly confused. Cited.")]
    + LONGFORM_SECTIONS["course"][3:]
)

_EXAM_RE = re.compile(
    r"\b(?:exams?|certifications?|certified|certificate|practice tests?|cca-f)\b",
    re.IGNORECASE,
)


def longform_profile(meta=None) -> str:
    """`exam-prep` when the source's metadata names an exam/certification, else `course`."""
    if meta is None:
        return "course"
    get = meta.get if isinstance(meta, dict) else (lambda k, d=None: getattr(meta, k, d))
    text = " ".join([get("title", "") or "", get("description", "") or "",
                     " ".join(get("tags", []) or [])])
    return "exam-prep" if _EXAM_RE.search(text) else "course"


# Prompt variant "current" (core/variants.py) adds People right after the
# vocabulary section; "legacy" is the list above unchanged.
PEOPLE_SECTION = ("People",
                  "The table described under People below: who speaks or is "
                  "discussed, their role here, background as stated, relevance. Cited.")


def longform_sections(profile: str, variant: Optional[str] = None) -> List[tuple]:
    """(heading, instruction) pairs for one long-form profile, in output order."""
    from video2mdnotes.core import variants
    out = list(LONGFORM_SECTIONS.get(profile, LONGFORM_SECTIONS["course"]))
    if variants.has_people(variant):
        k = next((i for i, (h, _) in enumerate(out) if h.startswith("Key Concepts")), 0)
        out.insert(k + 1, PEOPLE_SECTION)
    return out
