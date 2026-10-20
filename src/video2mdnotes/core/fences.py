"""Code-fence balance check and repair for assembled notes. Deterministic.

Why this exists (WP-D1): the T8 run of an 8,956-word Obsidian note produced
95 ``` fence lines — an odd count. One fence was never closed, so in Obsidian
everything after it rendered as one giant code block: every later heading,
table and citation link, gone from the reader's view, while the file looked
healthy by word count and exit code.

The rules follow CommonMark closely enough for notes:

* A fence line is optional indentation, then a run of 3+ backticks or 3+
  tildes, then an optional info string (a backtick fence's info string may not
  contain a backtick — "```x```" at line start is inline code, not a fence).
* Inside a fence, only a run of the SAME character, at least as long as the
  opener, with nothing but whitespace after it, closes it.

Two signals mean a fence was left open:

1. End of file while still inside a fence.
2. Inside a fence, a line that is itself an *opener with an info string*
   (```` ```python ````). A code block almost never contains a fenced opener
   with a language tag; a model that forgot to close the previous block
   produces exactly this. (Markdown-about-markdown examples nest with longer
   fences or ``~~~`` and are therefore not affected.)

The repair closes the open fence "at the end of the block it belongs to": the
first markdown heading after the opener (an ``##``-or-deeper heading is
preferred, because ``# comment`` is ordinary bash/python inside code),
otherwise just before the line that exposed the problem. Trailing blank lines
stay outside the fence, and the closer reuses the opener's indentation so a
fence opened inside a list item stays in the list item.

A bare opener with nothing but blank lines before its boundary is a stray
fence (a model's dangling closer), and is removed rather than turned into an
empty code block.

The guarantee callers rely on: after `repair_fences`, the number of fence
lines is even and `fence_report(text).balanced` is True.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from loguru import logger

_FENCE_LINE = re.compile(r"^(?P<indent>[ \t]*)(?P<run>`{3,}|~{3,})(?P<info>.*)$")
_ANY_HEADING = re.compile(r"^#{1,6}[ \t]+\S")
_DEEP_HEADING = re.compile(r"^#{2,6}[ \t]+\S")


@dataclass
class Fence:
    line: int            # 0-based line index
    indent: str
    char: str
    length: int
    info: str


def _parse_fence(line: str) -> Optional[Fence]:
    m = _FENCE_LINE.match(line)
    if not m:
        return None
    run, info = m.group("run"), m.group("info")
    if run[0] == "`" and "`" in info:
        return None                     # inline code span, not a fence
    return Fence(line=-1, indent=m.group("indent"), char=run[0],
                 length=len(run), info=info.strip())


def _closes(opener: Fence, line: str) -> bool:
    f = _parse_fence(line)
    return (f is not None and f.char == opener.char and f.length >= opener.length
            and not f.info)


@dataclass
class FenceReport:
    fence_lines: int = 0
    unclosed: List[int] = field(default_factory=list)   # 1-based opener lines

    @property
    def balanced(self) -> bool:
        return not self.unclosed and self.fence_lines % 2 == 0


def _scan(lines: List[str]) -> Tuple[int, List[Tuple[Fence, int]]]:
    """(fence line count, [(unclosed opener, index of the line that exposed it)]).

    The exposing index is either a reopening line (signal 2) or len(lines).
    """
    count = 0
    problems: List[Tuple[Fence, int]] = []
    open_: Optional[Fence] = None
    for i, line in enumerate(lines):
        f = _parse_fence(line)
        if f is None:
            continue
        if open_ is None:
            f.line = i
            open_ = f
            count += 1
        elif _closes(open_, line):
            open_ = None
            count += 1
        elif f.char == open_.char and f.length >= open_.length and f.info:
            problems.append((open_, i))
            f.line = i
            open_ = f
            count += 1
        # else: content inside the open fence
    if open_ is not None:
        problems.append((open_, len(lines)))
    return count, problems


def fence_report(text: str) -> FenceReport:
    count, problems = _scan((text or "").split("\n"))
    return FenceReport(fence_lines=count, unclosed=[p.line + 1 for p, _ in problems])


def count_fence_lines(text: str) -> int:
    return _scan((text or "").split("\n"))[0]


def _boundary(lines: List[str], opener: Fence, stop: int) -> int:
    """Index before which the closer goes: first heading after the opener."""
    for pattern in (_DEEP_HEADING, _ANY_HEADING):
        for j in range(opener.line + 1, stop):
            if pattern.match(lines[j]):
                return j
    return stop


def repair_fences(text: str, label: str = "notes") -> Tuple[str, List[str]]:
    """Close (or drop) every unclosed fence. Returns (text, repair log lines)."""
    if not text:
        return text, []
    lines = text.split("\n")
    log: List[str] = []
    # Repair one problem at a time and rescan: closing one fence changes how
    # every later fence line pairs up.
    for _ in range(len(lines) + 1):
        _, problems = _scan(lines)
        if not problems:
            break
        opener, stop = problems[0]
        at = _boundary(lines, opener, stop)
        end = at
        while end > opener.line + 1 and not lines[end - 1].strip():
            end -= 1
        fence_text = lines[opener.line].strip()
        if at < stop:
            where = f"before heading {lines[at].strip()[:60]!r}"
        elif stop >= len(lines):
            where = "at end of file"
        else:
            where = f"before the next fence opener {lines[stop].strip()[:40]!r}"
        if end == opener.line + 1 and not opener.info:
            del lines[opener.line]
            log.append(f"line {opener.line + 1}: removed stray fence {fence_text!r} "
                       f"(nothing inside it, {where})")
        else:
            lines.insert(end, f"{opener.indent}{opener.char * opener.length}")
            log.append(f"line {opener.line + 1}: closed unclosed fence {fence_text!r} "
                       f"at line {end + 1} ({where})")
    out = "\n".join(lines)
    for entry in log:
        logger.warning(f"{label}: code fence repaired — {entry}")
    return out, log
