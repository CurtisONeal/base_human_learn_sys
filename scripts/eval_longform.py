#!/usr/bin/env python3
"""Evaluate a long-form run directory against P1-P7 and emit the P8 sample.

Usage:
    .venv/bin/python scripts/eval_longform.py <run_dir> [--expected-chapters FILE]
        [--out DIR] [--max-gap 120] [--criteria P3,P6] [--scan-transcript]

Writes eval_report.json, eval_report.md and p8_sample.json to --out
(default <run_dir>/eval). Exit 0 if P1-P7 all pass, 1 otherwise. No model is
called; P8 is left "pending reviewer". The run-directory contract is in the
docstring of video2mdnotes.eval.longform.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from video2mdnotes.eval.longform import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
