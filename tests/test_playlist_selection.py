"""process --limit / --items / --since / --until / --dry-run-list (mocked yt-dlp).

The property that matters: a 1,202-entry playlist is never probed in full.
The flat listing is a generator; we count how many entries it yielded and
how many full per-entry metadata fetches were made."""

import datetime as dt
import re

import pytest
from typer.testing import CliRunner

from video2mdnotes.core import playlist as pl

N = 1202
START = dt.date(2026, 10, 20)


def _date(i):            # newest first: 1 entry per day
    return START - dt.timedelta(days=i - 1)


class FakeYT:
    """Flat listing + per-entry info, with counters."""

    def __init__(self, n=N, newest_first=True):
        self.n, self.newest_first = n, newest_first
        self.yielded = 0
        self.info_calls = []

    def date(self, i):
        return _date(i) if self.newest_first else _date(self.n + 1 - i)

    def entries(self):
        for i in range(1, self.n + 1):
            self.yielded += 1
            yield {"_type": "url", "ie_key": "Youtube", "id": f"vid{i:05d}",
                   "url": f"https://www.youtube.com/watch?v=vid{i:05d}",
                   "title": f"Episode {i}", "duration": 1500 + i}

    def list_fn(self, url):
        return {"_type": "playlist", "id": "PLX", "title": "The AI Breakdown",
                "playlist_count": self.n, "entries": self.entries()}

    def info_fn(self, url):
        self.info_calls.append(url)
        i = int(url.rsplit("vid", 1)[1])
        return {"id": f"vid{i:05d}", "title": f"Episode {i}", "duration": 1500 + i,
                "upload_date": self.date(i).strftime("%Y%m%d"),
                "webpage_url": url, "description": "d", "tags": [],
                "subtitles": {}, "automatic_captions": {"en": [{}]}}


def test_parse_items():
    assert pl.parse_items("1-3,7") == [(1, 3), (7, 7)]
    assert pl.parse_items("10-") == [(10, None)]
    assert pl.parse_items("2:4") == [(2, 4)]
    assert pl.items_max(pl.parse_items("1-3,7")) == 7
    assert pl.items_max(pl.parse_items("5,10-")) is None
    for bad in ("-3", "0", "3-1", "a", "1:5:2", ""):
        with pytest.raises(ValueError):
            pl.parse_items(bad)


def test_limit_reads_only_n_entries_and_fetches_nothing_extra():
    yt = FakeYT()
    listing = pl.select("u", pl.Selection(limit=3), list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert [e.index for e in listing.entries] == [1, 2, 3]
    assert yt.yielded == 3 and yt.info_calls == []
    assert listing.playlist_title == "The AI Breakdown"
    srcs = pl.to_sources(listing, info_fn=yt.info_fn)
    assert len(yt.info_calls) == 3                       # only the selected ones
    assert srcs[0].playlist_title == "The AI Breakdown" and srcs[0].title == "Episode 1"


def test_items_stop_at_last_position():
    yt = FakeYT()
    listing = pl.select("u", pl.Selection(items="1-3,7"), list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert [e.index for e in listing.entries] == [1, 2, 3, 7]
    assert yt.yielded == 8 and yt.info_calls == []       # 8th read to see it's past 7
    assert "past the last --items" in listing.stopped


def test_items_and_limit_combine():
    yt = FakeYT()
    listing = pl.select("u", pl.Selection(items="5-", limit=2), list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert [e.index for e in listing.entries] == [5, 6]
    assert yt.yielded == 6


def test_since_stops_early_on_newest_first():
    yt = FakeYT()
    since = START - dt.timedelta(days=4)                 # entries 1..5 match
    listing = pl.select("u", pl.Selection(since=since), list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert [e.index for e in listing.entries] == [1, 2, 3, 4, 5]
    assert listing.order == "newest-first"
    assert len(yt.info_calls) == 5 + pl.STOP_AFTER_OLDER   # dates fetched for candidates only
    assert yt.yielded == 5 + pl.STOP_AFTER_OLDER
    assert "newest-first" in listing.stopped
    # the metadata fetched while selecting is reused: no second fetch per source
    pl.to_sources(listing, info_fn=yt.info_fn)
    assert len(yt.info_calls) == 5 + pl.STOP_AFTER_OLDER


def test_until_and_since_window():
    yt = FakeYT()
    sel = pl.Selection(since=START - dt.timedelta(days=9), until=START - dt.timedelta(days=7))
    listing = pl.select("u", sel, list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert [e.index for e in listing.entries] == [8, 9, 10]
    assert yt.yielded == 10 + pl.STOP_AFTER_OLDER


def test_oldest_first_is_read_to_the_end_but_capped(monkeypatch):
    monkeypatch.setattr(pl, "MAX_DATE_PROBES", 50)
    yt = FakeYT(newest_first=False)
    listing = pl.select("u", pl.Selection(since=START - dt.timedelta(days=2)),
                        list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert len(yt.info_calls) == 50 and "cap" in listing.stopped
    assert listing.warnings


def test_dry_run_dates_only_for_selected():
    yt = FakeYT()
    listing = pl.select("u", pl.Selection(limit=3), need_dates=True,
                        list_fn=yt.list_fn, info_fn=yt.info_fn)
    assert len(yt.info_calls) == 3
    text = pl.render_listing(listing, pl.Selection(limit=3))
    assert re.search(r"vid00001 +2026-10-20", text) and "Episode 3" in text
    assert "1202 entries" in text


def test_single_video_url():
    def list_fn(url):
        return {"id": "abc", "title": "One video", "duration": 60, "upload_date": "20261001",
                "webpage_url": url}
    listing = pl.select("https://youtu.be/abc", pl.Selection(limit=5), list_fn=list_fn,
                        info_fn=lambda u: pytest.fail("no fetch needed"))
    assert not listing.is_playlist and [e.id for e in listing.entries] == ["abc"]


def test_bad_dates_and_limit():
    with pytest.raises(ValueError):
        pl.parse_date("2026/10/01", "--since")
    with pytest.raises(ValueError):
        pl.Selection(limit=0)
    with pytest.raises(ValueError):
        pl.Selection(since=dt.date(2026, 10, 2), until=dt.date(2026, 10, 1))


def test_cli_dry_run_list_downloads_nothing(monkeypatch):
    from video2mdnotes import main as m
    yt = FakeYT()
    monkeypatch.setattr(pl, "default_list", yt.list_fn)
    monkeypatch.setattr(pl, "default_info", yt.info_fn)
    monkeypatch.setattr(m, "probe_source", lambda *a, **k: pytest.fail("full probe"))
    monkeypatch.setattr(m, "fetch_audio", lambda *a, **k: pytest.fail("download"))
    r = CliRunner().invoke(m.app, ["process", "https://www.youtube.com/playlist?list=PLX",
                                   "--dry-run-list", "--limit", "3"])
    assert r.exit_code == 0, r.output
    assert "vid00003" in r.output and "vid00004" not in r.output
    assert yt.yielded == 3 and len(yt.info_calls) == 3
    r = CliRunner().invoke(m.app, ["process", "https://www.youtube.com/playlist?list=PLX",
                                   "--dry-run-list", "--since", "yesterday"])
    assert r.exit_code == 2
