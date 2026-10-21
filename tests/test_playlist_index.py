"""playlist-index: flat listing + cached, polite, resumable per-entry metadata. No network."""

import json
import urllib.error

from video2mdnotes.core import playlist_index as pi


def _listing(n=5, gone=()):
    return {"_type": "playlist", "id": "PLX", "title": "Show",
            "entries": [None if i in gone else {"id": f"vid{i:08d}", "title": f"Ep [{i}] | x",
                                                "duration": 600 + i}
                        for i in range(1, n + 1)]}


class Meta:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    def __call__(self, vid):
        self.calls.append(vid)
        exc = self.fail.get(vid)
        if exc is not None:
            if isinstance(exc, list):
                if exc:
                    raise exc.pop(0)
            else:
                raise exc
        n = int(vid[3:])
        return {"upload_date": f"2026-10-{30 - n:02d}",
                "description": f"Episode {n} is about things. More text https://x.y/z here.",
                "via": "test"}


def _build(tmp_path, meta, **kw):
    sleeps = []
    listing = kw.pop("listing", {})
    res = pi.build_index("https://www.youtube.com/playlist?list=PLX", tmp_path / "c.json",
                         list_fn=lambda u: _listing(**listing), meta_fn=meta,
                         sleep=sleeps.append, **kw)
    return res, sleeps


def test_parse_watch_page():
    html = ('..."uploadDate":"2026-10-19T09:41:01-07:00"...'
            '"shortDescription":"A new essay \\u2014 hailed.\\nSecond line"...')
    got = pi.parse_watch_page(html)
    assert got == {"upload_date": "2026-10-19", "description": "A new essay — hailed.\nSecond line"}
    assert pi.parse_watch_page("<html></html>")["upload_date"] is None


def test_first_sentence_drops_links_and_caps():
    assert pi.first_sentence("Big news today. Then more.") == "Big news today."
    assert pi.first_sentence("See https://a.b/c now! Next") == "See now!"
    assert pi.first_sentence("x " * 300).endswith("…")
    assert pi.first_sentence(None) == ""


def test_fetch_all_then_resume_from_cache(tmp_path):
    meta = Meta()
    res, sleeps = _build(tmp_path, meta, max_fetches=2)
    assert res.fetched == 2 and res.stopped.startswith("--max-fetches")
    assert len(sleeps) == 1                                  # polite: a pause between requests
    meta2 = Meta()
    res2, _ = _build(tmp_path, meta2)
    assert res2.from_cache == 2 and res2.fetched == 3
    assert meta2.calls == ["vid00000003", "vid00000004", "vid00000005"]   # only what is missing
    assert res2.dated == 5
    meta3 = Meta()
    res3, _ = _build(tmp_path, meta3)
    assert meta3.calls == [] and res3.from_cache == 5


def test_backoff_on_429_and_private_videos_not_retried(tmp_path):
    err429 = urllib.error.HTTPError("u", 429, "Too Many", {}, None)
    meta = Meta(fail={"vid00000002": [err429, err429],
                      "vid00000004": RuntimeError("ERROR: [youtube] vid00000004: Private video")})
    res, sleeps = _build(tmp_path, meta)
    assert meta.calls.count("vid00000002") == 3 and res.fetched == 4
    assert pi.BACKOFF_S in sleeps and pi.BACKOFF_S * 2 in sleeps
    assert res.gone == 1 and res.fetch_failed == 1
    cache = json.loads((tmp_path / "c.json").read_text())
    assert cache["entries"]["vid00000004"]["unavailable"] is True
    meta2 = Meta()
    _build(tmp_path, meta2)
    assert meta2.calls == []                                 # the private video is not retried


def test_throttle_stops_and_keeps_cache(tmp_path):
    meta = Meta(fail={f"vid{i:08d}": RuntimeError("boom") for i in range(1, 30)})
    res, _ = _build(tmp_path, meta, listing={"n": 20})
    assert "consecutive failures" in res.stopped
    assert len(meta.calls) == pi.MAX_CONSECUTIVE_FAILURES
    assert (tmp_path / "c.json").exists()


def test_render_status_links_and_escaping(tmp_path):
    subject = tmp_path / "vault" / "subj"
    pkg = subject / "ep_two"
    pkg.mkdir(parents=True)
    (pkg / "notes.md").write_text("# x")
    (pkg / "source_meta.json").write_text(json.dumps({"source": {"youtube_id": "vid00000002"}}))
    runs = tmp_path / "runs" / "r1"
    (runs / "summaries").mkdir(parents=True)
    (runs / "summaries" / "a.summary.md").write_text("x")
    (runs / "source_meta.json").write_text(json.dumps({"source_id": "vid00000003"}))
    res, _ = _build(tmp_path, Meta(), listing={"n": 4, "gone": (4,)})
    out = subject / "_index.md"
    pi.apply_status(res, out, subject, tmp_path / "runs")
    text = pi.render_index(res, built_at="2026-10-21T00:00:00")
    assert "count: 3" in text and "notes_done: 2" in text and "packaged: 1" in text
    assert "built_at: 2026-10-21T00:00:00" in text and 'url: "https://www.youtube.com/playlist?list=PLX"' in text
    row2 = next(ln for ln in text.split("\n") if ln.startswith("| 2 |"))
    assert "[packaged](ep_two/notes.md)" in row2 and "| 2026-10-28 |" in row2
    assert "[Ep (2) \\| x](https://www.youtube.com/watch?v=vid00000002)" in row2
    assert "Episode 2 is about things." in row2 and "https://x.y" not in row2
    row3 = next(ln for ln in text.split("\n") if ln.startswith("| 3 |"))
    assert "| notes |" in row3
    assert res.unavailable == 1
