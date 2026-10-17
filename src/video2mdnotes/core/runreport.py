"""Machine-readable run reporting (WP-M3).

A test harness once guessed a run folder from the video title with the glob
``*let_s_build_gpt*``; the pipeline had sanitized "Let's" to "lets", the glob
matched nothing, and the evaluation silently ran on nothing. The fix is at
the source: every command SAYS where its run went.

* stdout, last lines: one ``RUN_DIR=<absolute path>`` line per run dir the
  command produced (or found already done and skipped), printed with plain
  ``print`` — never through rich, which wraps long paths.
* ``<output_dir>/_last_run.json``: ``{run_dir, command, source, started,
  finished, status, runs: [...]}`` — ``run_dir`` is the last run's.

Callers never derive a run dir from a title; they read one of these.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from video2mdnotes.config import settings

LAST_RUN_FILENAME = "_last_run.json"
RUN_DIR_PREFIX = "RUN_DIR="


def last_run_path() -> Path:
    return Path(settings.last_run_file or (Path(settings.output_dir) / LAST_RUN_FILENAME))


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


@dataclass
class RunReport:
    command: str
    source: str
    started: str = field(default_factory=_now)
    runs: List[dict] = field(default_factory=list)

    def add(self, run_dir: Path, status: str, source: Optional[str] = None) -> None:
        """Record one run dir. A later entry for the same dir replaces the
        earlier (a dir renamed afterwards is recorded under its final name by
        calling `rename`). A dir that does not exist was not produced and is
        not recorded."""
        if not Path(run_dir).expanduser().exists():
            return
        path = str(Path(run_dir).expanduser().resolve())
        self.runs = [r for r in self.runs if r["run_dir"] != path]
        self.runs.append({"run_dir": path, "status": status,
                          "source": source if source is not None else self.source})

    def rename(self, old: Path, new: Path) -> None:
        old_s = str(Path(old).expanduser().resolve())
        for r in self.runs:
            if r["run_dir"] == old_s:
                r["run_dir"] = str(Path(new).expanduser().resolve())

    def overall(self) -> str:
        statuses = {r["status"] for r in self.runs}
        if not statuses:
            return "no_run"
        for bad in ("stopped", "failed"):
            if bad in statuses:
                return bad
        return "ok"

    def record(self, status: Optional[str] = None) -> dict:
        return {
            "run_dir": self.runs[-1]["run_dir"] if self.runs else None,
            "command": self.command,
            "source": self.source,
            "started": self.started,
            "finished": _now(),
            "status": status or self.overall(),
            "runs": list(self.runs),
        }

    def emit(self, status: Optional[str] = None, stream=None) -> dict:
        """Write _last_run.json, then print the RUN_DIR lines (last on stdout)."""
        rec = self.record(status)
        path = last_run_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:  # reporting must never hide the run's own result
            print(f"warning: could not write {path}: {exc}", file=sys.stderr)
        out = stream or sys.stdout
        for r in self.runs:
            print(f"{RUN_DIR_PREFIX}{r['run_dir']}", file=out, flush=True)
        return rec


def parse_run_dirs(text: str) -> List[str]:
    """Every RUN_DIR= path printed in `text` (for callers and tests)."""
    return [line[len(RUN_DIR_PREFIX):].strip() for line in (text or "").splitlines()
            if line.startswith(RUN_DIR_PREFIX)]
