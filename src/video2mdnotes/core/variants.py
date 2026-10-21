"""Summary prompt variants, for A/B tests on the same transcript.

``current`` — the prompts with the People section (core/people.py,
prompts/fragments/people.md). ``legacy`` — exactly the prompts on main
before it (7fcd1d7): no People section anywhere, byte-identical rendered
system prompts (guarded by tests/fixtures/prompt_variants/).

Chosen by ``PROMPT_VARIANT`` (settings) or ``--prompt-variant`` on
process / media / longform / book; recorded as ``prompt_variant`` in the
notes' frontmatter (documents: the provenance footer) and in
longform_state.json.
"""

from __future__ import annotations

from typing import Optional

from video2mdnotes.config import PROMPT_VARIANTS, settings

CURRENT = "current"
LEGACY = "legacy"


def normalize(value: Optional[str]) -> str:
    v = (value or CURRENT).strip().lower()
    if v not in PROMPT_VARIANTS:
        raise ValueError(f"prompt variant must be one of {', '.join(PROMPT_VARIANTS)}, not {value!r}")
    return v


def active() -> str:
    return normalize(getattr(settings, "prompt_variant", CURRENT))


def set_active(value: Optional[str]) -> str:
    """Validate and set the variant for this process (None keeps the setting)."""
    if value is None:
        return active()
    settings.prompt_variant = normalize(value)
    return settings.prompt_variant


def has_people(variant: Optional[str] = None) -> bool:
    return normalize(variant or active()) == CURRENT


__all__ = ["CURRENT", "LEGACY", "active", "has_people", "normalize", "set_active"]
