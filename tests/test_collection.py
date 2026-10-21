"""Collections (core/collection.py): selection, resume, limits, retries, digests.

Mocked pipeline: no network, no model, no child process."""

import datetime as dt
import json
from pathlib import Path

import pytest

from video2mdnotes.core import collection as cm
from test_digest import FakeModel, _notes  # noqa: E402 - tests dir is on sys.path

PL = "https://www.youtube.com/playlist?list=PLtest"
# id, title, date, duration_s  (newest first, like the AI Daily Brief)
EPS = [
    ("CCCCCCCCCCC", "Fund update", "2026-10-21", 1500),
    ("BBBBBBBBBBB", "Fund debate", "2026-10-19", 1700),
    ("AAAAAAAAAAA", "Fund launched", "2026-10-18", 900),
    ("ZZZZZZZZZZZ", "Old robotics episode", "2026-10-01", 4000),
]


def _list_fn(eps=EPS):
    def f(url):
        return {"_type": "playlist", "id": "PLtest", "title": "Test Show",
                "entries": [{"id": i, "title": t, "duration": d} for i, t, _, d in eps]}
    return f


def _seed_cache(output_root, eps=EPS):
    p = output_root / "_playlist_cache" / "PLtest.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"version": 1, "entries": {
        i: {"upload_date": d, "description": f"About {t.lower()} and robots" if "robot" in t.lower()
            else f"About {t.lower()}"} for i, t, d, _ in eps}}))


def _no_meta(_id):
    raise AssertionError("network: watch page fetched although cached")


@pytest.fixture
def col(tmp_path):
    out = tmp_path / "out"
    _seed_cache(out)
    c = cm.Collection.create(out / "collections", "show", subject="show",
                             sources=[{"kind": "playlist", "url": PL}])
    cm.build_listing(c, output_root=out, list_fn=_list_fn(), meta_fn=_no_meta)
    c.save()
    return c


def _select(c, **kw):
    f = cm.Filters(**kw)
    m = cm.select_entries(cm.load_listing(c), f)
    cm.apply_selection(c, m, f)
    return m


# --------------------------------------------------------------------------- #
# Fake pipeline
# --------------------------------------------------------------------------- #

def _write_run(run_dir, it, calls=1):
    run_dir = Path(run_dir)
    (run_dir / "summaries").mkdir(parents=True, exist_ok=True)
    (run_dir / "transcripts").mkdir(exist_ok=True)
    (run_dir / "transcripts" / "t.md").write_text("- [0:00:01.000] hi\n")
    yid = it["id"]
    text = _notes(it["title"], it["published"], yid,
                  [("NLW", "host", "Host", "Explains", "0:00:10")],
                  [("A fact", "0:13:29"), ("Other", "0:09:54")],
                  [("Public Benefit Fund", "An LLC", "0:13:29")],
                  [("The fund holds 5%", "0:13:29")], linked=False)
    (run_dir / "summaries" / "notes.summary.md").write_text(text)
    (run_dir / "source_meta.json").write_text(json.dumps(
        {"source_id": yid, "title": it["title"], "url": f"https://www.youtube.com/watch?v={yid}",
         "published_at": it["published"], "duration_s": 1500.0}))
    (run_dir / "longform_state.json").write_text(json.dumps({"passages": {
        f"p{n}": {"status": "done", "model": "claude-cli/opus"} for n in range(calls)}}))


class FakeRunner:
    def __init__(self, script=None):
        self.calls = []
        self.script = dict(script or {})   # id -> list of outcomes to return first

    def __call__(self, it, run_dir):
        self.calls.append(it["id"])
        todo = self.script.get(it["id"])
        if todo:
            kind = todo.pop(0)
            if kind == "interrupt":
                raise KeyboardInterrupt
            if kind == "exhausted":
                return cm.Outcome("exhausted", run_dir, "out", reset_at=None)
            return cm.Outcome("failed", run_dir, "boom")
        _write_run(run_dir, it)
        return cm.Outcome("noted", run_dir)


def _run(c, runner, lines=None, **kw):
    lines = lines if lines is not None else []
    kw.setdefault("evaluate", False)
    limits = kw.pop("limits", cm.Limits(backoff_s=1))
    return cm.run_collection(c, limits, runner=runner, emit=lines.append, sleep=kw.pop("sleep", lambda s: None),
                             **kw)


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #

def test_selection_from_cached_index_no_network(col):
    entries = cm.load_listing(col)
    assert [e["id"] for e in entries][:1] == ["CCCCCCCCCCC"]
    # --last-days 7 as of 2026-10-21: since 2026-10-14 .. 2026-10-21
    f = cm.Filters(last_days=7, as_of=dt.date(2026, 10, 21))
    assert (f.since, f.until) == (dt.date(2026, 10, 14), dt.date(2026, 10, 21))
    m = cm.select_entries(entries, f)
    assert [e["id"] for e in m] == ["CCCCCCCCCCC", "BBBBBBBBBBB", "AAAAAAAAAAA"]
    # regex on description, durations, limit (playlist order)
    assert [e["id"] for e in cm.select_entries(entries, cm.Filters(match="robot"))] == ["ZZZZZZZZZZZ"]
    assert [e["id"] for e in cm.select_entries(
        entries, cm.Filters(min_duration_s=cm.parse_duration("20m"),
                            max_duration_s=cm.parse_duration("0:30:00")))] == ["CCCCCCCCCCC", "BBBBBBBBBBB"]
    assert [e["id"] for e in cm.select_entries(entries, cm.Filters(limit=2))] == ["CCCCCCCCCCC", "BBBBBBBBBBB"]
    assert [e["id"] for e in cm.select_entries(entries, cm.Filters(items="2-3"))] == ["BBBBBBBBBBB", "AAAAAAAAAAA"]
    # queue is oldest first; re-selecting adds nothing
    r = cm.apply_selection(col, m, f)
    assert r["added"] == 3
    assert [i["id"] for i in col.queue()] == ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"]
    assert all(i["state"] == "queued" for i in col.queue())
    assert cm.apply_selection(col, m, f)["added"] == 0
    # a fresh load sees the same manifest
    again = cm.Collection.load(col.dir.parent, "show")
    assert set(again.items) == set(col.items)


def test_parse_duration_and_deadline():
    assert cm.parse_duration("1h30m") == 5400
    assert cm.parse_duration("25") == 1500
    assert cm.parse_duration("1:05:00") == 3900
    with pytest.raises(ValueError):
        cm.parse_duration("abc")
    now = dt.datetime(2026, 10, 21, 23, 0)
    assert cm.until_deadline("06:00", now) == dt.datetime(2026, 10, 22, 6, 0)
    assert cm.until_deadline("23:30", now) == dt.datetime(2026, 10, 21, 23, 30)


# --------------------------------------------------------------------------- #
# Run
# --------------------------------------------------------------------------- #

def test_run_resume_after_interruption(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    r1 = FakeRunner({"BBBBBBBBBBB": ["interrupt"]})
    lines = []
    s1 = _run(col, r1, lines)
    assert s1.stop_reason == "interrupted"
    assert r1.calls == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert col.items["AAAAAAAAAAA"]["state"] == "noted"
    assert col.items["BBBBBBBBBBB"]["state"] == "queued"
    assert col.items["BBBBBBBBBBB"]["attempts"] == 0          # an interrupt is not a failure
    assert any(ln.startswith("COLLECTION_ITEM_DONE=AAAAAAAAAAA ") for ln in lines)
    assert "COLLECTION_STOPPED=interrupted" in lines
    assert not (col.dir / cm.LOCK).exists()
    # resume: only the unfinished items run
    reloaded = cm.Collection.load(col.dir.parent, "show")
    r2 = FakeRunner()
    s2 = _run(reloaded, r2)
    assert r2.calls == ["BBBBBBBBBBB", "CCCCCCCCCCC"]
    assert not s2.stop_reason
    # a third run is a no-op
    r3 = FakeRunner()
    _run(cm.Collection.load(col.dir.parent, "show"), r3)
    assert r3.calls == []
    last = json.loads((col.dir / cm.LAST_RUN).read_text())
    assert last["processed"] == []


def test_max_calls_and_max_items_stop_between_items(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    r = FakeRunner()
    s = _run(col, r, limits=cm.Limits(max_calls=2))
    assert r.calls == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert s.calls == 2
    assert s.stop_reason.startswith("--max-calls 2")
    assert col.items["CCCCCCCCCCC"]["state"] == "queued"
    r2 = FakeRunner()
    s2 = _run(col, r2, limits=cm.Limits(max_items=0))
    assert r2.calls == [] and s2.stop_reason.startswith("--max-items 0")


def test_until_time_stops_before_starting(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    r = FakeRunner()
    past = dt.datetime.now() - dt.timedelta(minutes=1)
    s = _run(col, r, limits=cm.Limits(deadline=past))
    assert r.calls == [] and s.stop_reason.startswith("until-time")


def test_failure_retry_with_backoff_then_failed(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    slept = []
    r = FakeRunner({"AAAAAAAAAAA": ["fail"], "BBBBBBBBBBB": ["fail", "fail", "fail"]})
    s = _run(col, r, sleep=slept.append, limits=cm.Limits(max_attempts=3, backoff_s=10))
    a, b = col.items["AAAAAAAAAAA"], col.items["BBBBBBBBBBB"]
    assert a["state"] == "noted" and a["attempts"] == 2 and a["error"] is None
    assert b["state"] == "failed" and b["attempts"] == 3 and b["error"] == "boom"
    assert s.failed == ["BBBBBBBBBBB"] and "AAAAAAAAAAA" in s.processed
    # A first, then B, C; retries are queued at the end, in order
    assert r.calls == ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC", "AAAAAAAAAAA",
                       "BBBBBBBBBBB", "BBBBBBBBBBB"]
    assert any(9 <= x <= 20 for x in slept)                     # exponential backoff slept
    # failed at max attempts: skipped next run unless --retry-failed
    r2 = FakeRunner()
    _run(col, r2)
    assert r2.calls == []
    _run(col, r2, limits=cm.Limits(retry_failed=True))
    assert r2.calls == ["BBBBBBBBBBB"] and col.items["BBBBBBBBBBB"]["state"] == "noted"


def test_exhaustion_waits_and_records_or_stops(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    slept = []
    r = FakeRunner({"AAAAAAAAAAA": ["exhausted"]})
    s = _run(col, r, sleep=slept.append, on_exhaustion="wait", exhaustion_wait_s=300)
    assert slept == [300]
    assert r.calls[:2] == ["AAAAAAAAAAA", "AAAAAAAAAAA"]
    assert col.items["AAAAAAAAAAA"]["attempts"] == 1
    assert s.waits and col.data["events"][-1]["kind"] == "exhausted"
    assert col.data["events"][-1]["waited_s"] == 300
    # policy stop: clean stop, item untouched
    _select(col, items="4")
    r2 = FakeRunner({"ZZZZZZZZZZZ": ["exhausted"]})
    s2 = _run(col, r2, on_exhaustion="stop")
    assert s2.stop_reason.startswith("subscription exhausted")
    assert col.items["ZZZZZZZZZZZ"]["state"] == "queued"


def test_reuses_existing_package_without_calling_pipeline(col, tmp_path):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    vault_subject = tmp_path / "vault" / "show"
    old_run = tmp_path / "old_run"
    _write_run(old_run, col.items["BBBBBBBBBBB"], calls=1)
    pkg = vault_subject / "fund_debate"
    pkg.mkdir(parents=True)
    (pkg / "notes.md").write_text("x")
    (pkg / "source_meta.json").write_text(json.dumps(
        {"run_dir": str(old_run), "source": {"youtube_id": "BBBBBBBBBBB"}}))
    packaged = []

    def packager(run_dir, subject):
        packaged.append(Path(run_dir))
        d = vault_subject / Path(run_dir).name
        d.mkdir(parents=True, exist_ok=True)
        return {"package_dir": str(d), "linked": 3, "total": 3, "unresolved": 0, "broken": 0}

    r = FakeRunner()
    lines = []
    s = _run(col, r, lines, package_subject="show", vault_subject_dir=vault_subject,
             packager=packager)
    assert r.calls == ["AAAAAAAAAAA", "CCCCCCCCCCC"]
    b = col.items["BBBBBBBBBBB"]
    assert b["state"] == "packaged" and b["reused"] is True and b["run_dir"] == str(old_run)
    assert b["calls"] == 1                                      # read from its checkpoints
    assert s.reused == ["BBBBBBBBBBB"] and len(packaged) == 2
    assert f"COLLECTION_ITEM_DONE=BBBBBBBBBBB {pkg}" in lines


def test_noted_item_only_packaged_on_rerun(col):
    _select(col, items="3")
    _run(col, FakeRunner())                       # notes, no packaging
    assert col.items["AAAAAAAAAAA"]["state"] == "noted"
    r = FakeRunner()
    calls = []
    _run(col, r, package_subject="show", vault_subject_dir=col.dir / "nope",
         packager=lambda d, s: calls.append(d) or {"package_dir": str(d), "linked": 1, "total": 1,
                                                    "unresolved": 0, "broken": 0})
    assert r.calls == [] and len(calls) == 1
    assert col.items["AAAAAAAAAAA"]["state"] == "packaged"


def test_lock_refuses_concurrent_run(col):
    (col.dir / cm.LOCK).parent.mkdir(parents=True, exist_ok=True)
    (col.dir / cm.LOCK).write_text("1")                # pid 1 is always alive
    with pytest.raises(cm.Locked):
        _run(col, FakeRunner())


# --------------------------------------------------------------------------- #
# Digests: only affected spans
# --------------------------------------------------------------------------- #

def test_digests_rebuilt_only_for_affected_spans(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    seen = []

    def digester(collection_dir, by, keys, **kw):
        seen.append(list(keys))
        out = Path(collection_dir) / "digests"
        out.mkdir(parents=True, exist_ok=True)
        for k in keys:
            (out / f"{k}.md").write_text("d")

            class R:
                span, path, episodes, calls, cached, citations, dropped = k, out / f"{k}.md", 1, 1, 0, 3, 0
                link_problems, partial, written = [], True, True
            kw["on_built"](R)
        return [], []

    lines = []
    # 2026-10-18 is W42 (Sunday); 2026-10-19 and -21 are W43
    s = _run(col, FakeRunner(), lines, digest_by="week", digester=digester, limits=cm.Limits(max_items=1))
    assert seen == [["2026-W42"]]
    assert "COLLECTION_DIGEST=2026-W42 " + str(col.runs_dir / "digests" / "2026-W42.md") in lines
    _run(col, FakeRunner(), digest_by="week", digester=digester)
    assert seen[-1] == ["2026-W43"]                             # W42 untouched
    n = len(seen)
    _run(col, FakeRunner(), digest_by="week", digester=digester)
    assert len(seen) == n                                       # nothing new: no digest work
    assert col.data["digests"]["week:2026-W43"]["link_check"] == "pass"


def test_default_digester_real_spans_and_cascade(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    _run(col, FakeRunner())
    model = FakeModel()
    res, left = cm.default_digester(col.runs_dir, "week", ["2026-W43"], complete=model,
                                    as_of=dt.date(2026, 10, 21))
    assert [r.span for r in res] == ["2026-W43"] and left == []
    assert len(model.calls) == 1 and not res[0].link_problems
    # W42 is (re)built → W43's "what changed" depends on it → cascade
    res, _ = cm.default_digester(col.runs_dir, "week", ["2026-W42"], complete=model,
                                 as_of=dt.date(2026, 10, 21))
    assert [r.span for r in res] == ["2026-W42", "2026-W43"]
    # rerun: cached, nothing written, no calls
    before = len(model.calls)
    res, _ = cm.default_digester(col.runs_dir, "week", ["2026-W42", "2026-W43"], complete=model,
                                 as_of=dt.date(2026, 10, 21))
    assert len(model.calls) == before and not any(r.written for r in res)
    # max_calls: nothing left in the budget → spans reported as left
    res, left = cm.default_digester(col.runs_dir, "week", ["2026-W42"], complete=model,
                                    max_calls=0)
    assert res == [] and left == ["2026-W42"]


def test_digest_pending_when_calls_run_out(col):
    _select(col, last_days=7, as_of=dt.date(2026, 10, 21))
    model = FakeModel()

    def digester(d, by, keys, **kw):
        return cm.default_digester(d, by, keys, complete=model, **kw)

    s = _run(col, FakeRunner(), digest_by="week", digester=digester, limits=cm.Limits(max_calls=3),
             as_of=dt.date(2026, 10, 21))
    assert s.calls == 3 and s.pending_digests == ["2026-W42", "2026-W43"]
    assert col.data["pending_digests"]["week"] == ["2026-W42", "2026-W43"]
    s2 = _run(col, FakeRunner(), digest_by="week", digester=digester, as_of=dt.date(2026, 10, 21))
    assert [d["span"] for d in s2.digests] == ["2026-W42", "2026-W43"]
    assert col.data["pending_digests"]["week"] == []


def test_subprocess_runner_command_resumes_with_longform(tmp_path):
    r = cm.SubprocessRunner(tmp_path / "logs", usd_cap=0.0)
    it = {"id": "AAAAAAAAAAA", "url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"}
    fresh = r.command(it, tmp_path / "run")
    assert fresh[3:6] == ["process", it["url"], "--out"]
    assert fresh[-4:] == ["--on-exhaustion", "stop", "--usd-cap", "0.0"]
    d = tmp_path / "run"
    (d / "transcripts").mkdir(parents=True)
    (d / "transcripts" / "t.md").write_text("- [0:00:01.000] hi\n")
    (d / "source_meta.json").write_text("{}")
    assert r.command(it, d)[3:5] == ["longform", str(d)]     # no re-download / re-ASR


def test_cli_init_select_status(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from video2mdnotes import main as m
    from video2mdnotes.config import settings
    from video2mdnotes.core import playlist, playlist_index
    out = tmp_path / "out"
    _seed_cache(out)
    monkeypatch.setattr(settings, "output_dir", out)
    monkeypatch.setattr(settings, "obsidian_vault", tmp_path / "vault")
    monkeypatch.setattr(playlist, "default_list", _list_fn())
    monkeypatch.setattr(playlist_index, "default_meta", _no_meta)
    run = CliRunner().invoke
    res = run(m.app, ["collection", "init", "show", "--playlist", PL, "--subject", "show"])
    assert res.exit_code == 0, res.output
    assert (out / "collections" / "show" / "collection.json").exists()

    def no_net(_u):
        raise AssertionError("network listing during select")
    monkeypatch.setattr(playlist, "default_list", no_net)
    res = run(m.app, ["collection", "select", "show", "--last-days", "7", "--as-of", "2026-10-21"])
    assert res.exit_code == 0, res.output
    assert "-> 3 episode(s)" in res.output and "est. 3 model call(s)" in res.output
    res = run(m.app, ["collection", "status", "show"])
    assert res.exit_code == 0 and "queued 3" in res.output and "est. 3 model call(s)" in res.output


def test_count_calls_from_checkpoints(tmp_path):
    d = tmp_path / "r"
    d.mkdir()
    (d / "longform_state.json").write_text(json.dumps({
        "passages": {"a": {"status": "done", "model": "m", "pack": "pack_01-02"},
                     "b": {"status": "done", "model": "m", "pack": "pack_01-02"},
                     "c": {"status": "done", "model": "m"},
                     "d": {"status": "failed", "model": None}},
        "assembly": {"course_map_calls": 1}, "consistency": {"called": True}}))
    assert cm.count_calls(d) == 4
    assert cm.estimate_calls(25 * 60) == 1
    assert cm.estimate_calls(3 * 3600) == 5
