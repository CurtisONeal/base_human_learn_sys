"""Span digests over existing episode notes (core/digest.py). Mocked model; no network."""

import datetime as dt
import json
import re

import pytest

from video2mdnotes.core import digest as dg


def _notes(title, published, yid, people_rows, summary, terms, claims, linked=True):
    def c(ts):
        if not linked:
            return f"[{ts}]"
        h, m, s = (int(x) for x in ts.split(":"))
        return f"[{ts}](https://youtu.be/{yid}?t={h * 3600 + m * 60 + s})"
    ppl = "\n".join(f"| {n} | {r} | {b} | {rel} | {c(t)} |" for n, r, b, rel, t in people_rows)
    sm = "\n".join(f"- {s} {c(t)}" for s, t in summary)
    tm = "\n".join(f"- `{n}` - {d} {c(t)}" for n, d, t in terms)
    cl = "\n".join(f"| {cl} | {c(t)} | {published} | news | the source |" for cl, t in claims)
    # packaged notes link the time range in the heading too (as package.py writes it)
    window = (f"[0:00:00–0:25:00](https://youtu.be/{yid}?t=0)" if linked else "[0:00:00–0:25:00]")
    return f"""---
title: "{title}"
source: "https://www.youtube.com/watch?v={yid}"
published: {published}
published_by: "The AI Daily Brief: Artificial Intelligence News"
duration_s: 1500.0
---
# {title}

## People

| person | role | affiliation & background (as stated) | relevance | citations |
|---|---|---|---|---|
{ppl}

## Window 1 {window}

### Summary

{sm}

### Key Concepts & Vocabulary

{tm}

### Special Attention

- Check the numbers {c("0:01:00")}

## Time-Sensitive Claims

| claim | timestamp | as-of | type | verify-by |
|---|---|---|---|---|
{cl}
"""


EPS = [
    # slug, title, date, id
    ("ep_a", "Fund launched", "2026-10-18", "AAAAAAAAAAA"),   # Sunday: week 42
    ("ep_b", "Fund debate", "2026-10-19", "BBBBBBBBBBB"),     # Monday: week 43
    ("ep_c", "Fund update", "2026-10-21", "CCCCCCCCCCC"),     # week 43
]


def _make(tmp_path, eps=EPS, linked=True):
    col = tmp_path / "ai_daily_brief"
    for slug, title, date, yid in eps:
        d = col / slug
        d.mkdir(parents=True)
        text = _notes(
            title, date, yid,
            [("NLW", "host", "Host of the show", "Explains the fund", "0:00:10"),
             ("Mark Levine (ASR spelling)", "quoted", "NYC Comptroller", "Wants a pause", "0:09:54")],
            [("The fund starts at 5%", "0:13:29"), ("Levine urges a pause", "0:09:54")],
            [("Public Benefit Fund", f"An LLC ({slug})", "0:13:29"),
             (f"Term {slug}", "Only here", "0:02:00")],
            [(f"The fund holds 5% ({slug})", "0:13:29")], linked=linked)
        (d / "notes.md").write_text(text, encoding="utf-8")
        (d / "source_meta.json").write_text(json.dumps(
            {"kind": "youtube", "title": title,
             "source": {"url": f"https://www.youtube.com/watch?v={yid}", "youtube_id": yid,
                        "published_at": date, "duration_s": 1500.0}}), encoding="utf-8")
    return col


class FakeModel:
    def __init__(self, extra_bad=False):
        self.calls = []
        self.extra_bad = extra_bad

    def __call__(self, system, user):
        self.calls.append((system, user))
        labels = re.findall(r"(E\d+) = ", user)
        cites = [f"{lab} 0:13:29" for lab in labels]
        d = {
            "title": "The fund",
            "overview": [{"text": "A fund dominated the span.", "cites": cites}],
            "top_stories": [
                {"headline": "Levine pause", "summary": "He wants a pause.",
                 "cites": [f"{labels[0]} 0:09:54"],
                 "segments": [{"episode": labels[0], "from": "0:09:54", "to": "0:11:00"}]},
                {"headline": "Anthropic fund", "summary": "5% of shares.", "cites": cites,
                 "segments": [{"episode": lab, "from": "0:13:29", "to": "0:20:00"} for lab in labels]},
            ],
            "threads": [{"thread": "Fund", "developments": [
                {"episode": lab, "text": f"Development in {lab}", "cites": [f"{lab} 0:13:29"]}
                for lab in labels], "latest": {"text": "Still debated", "cites": cites[-1:]}}],
            "people": [{"name": "Mark Levine", "relevance": "Pushed for a pause all week",
                        "cites": [f"{labels[0]} 0:09:54"]}],
            "superseded": ([{"earlier": "C1", "later": f"C{len(labels)}", "kind": "superseded",
                             "resolution": "updated"}] if len(labels) > 1 else []),
            "what_changed": [],
            "open_questions": [{"text": "Will the IPO pause?", "cites": cites[:1]}],
        }
        if self.extra_bad:
            d["open_questions"].append({"text": "Invented", "cites": ["E9 0:00:01", "E1 2:59:00"]})
            d["overview"].append({"text": "No cite at all", "cites": []})
        return json.dumps(d), "fake/model"


AS_OF = dt.date(2026, 10, 30)


def test_grouping_iso_weeks_and_months(tmp_path):
    col = _make(tmp_path)
    eps = dg.load_collection(col)
    weeks = dg.group(eps, "week")
    assert [s.key for s in weeks] == ["2026-W42", "2026-W43"]
    assert [len(s.episodes) for s in weeks] == [1, 2]       # Sunday 10-18 is still W42
    assert weeks[1].start == dt.date(2026, 10, 19) and weeks[1].end == dt.date(2026, 10, 25)
    months = dg.group(eps, "month")
    assert [s.key for s in months] == ["2026-10"] and len(months[0].episodes) == 3
    assert [s.key for s in dg.group(eps, "week", since=dt.date(2026, 10, 19))] == ["2026-W43"]


def test_package_links_are_unlinked_for_the_prompt(tmp_path):
    ep = dg.load_episode(_make(tmp_path) / "ep_b" / "notes.md")
    assert "youtu.be" not in ep.body and "[0:13:29]" in ep.body
    assert ep.youtube_id == "BBBBBBBBBBB" and ep.published == dt.date(2026, 10, 19)
    assert 809.0 in ep.timestamps and len(ep.people_rows) == 2 and len(ep.claims) == 1


def test_prompt_carries_labels_notes_and_claim_ids(tmp_path):
    col = _make(tmp_path)
    fake = FakeModel()
    dg.run(col, "week", complete=fake, as_of=AS_OF)
    system, user = fake.calls[-1]
    assert "E1 = 2026-10-19" in user and "E2 = 2026-10-21" in user
    assert "=== E1 · 2026-10-19" in user and "The fund starts at 5% [0:13:29]" in user
    assert "C1 · E1 0:13:29" in user and "C2 · E2 0:13:29" in user
    norm = " ".join(system.split()).lower()
    assert "never invent" in norm and "e<n> h:mm:ss" in norm


def test_citations_resolve_and_invalid_ones_are_dropped(tmp_path):
    col = _make(tmp_path)
    res = dg.run(col, "week", complete=FakeModel(extra_bad=True), as_of=AS_OF)
    w43 = res[1]
    assert w43.link_problems == []
    text = w43.path.read_text()
    assert "link_check: pass" in text
    oq = text.split("## Open questions")[1].split("\n## ")[0]
    ov = text.split("## Overview")[1].split("\n## ")[0]
    assert "Invented" not in oq and "No cite at all" not in ov
    check = text.split("## Citation check")[1]
    assert "dropped: open_questions[1]: E9 0:00:01" in check and "dropped: overview: No cite at all" in check
    assert "citations_dropped: 2" in text and "items_dropped: 2" in text
    # every YouTube link points at an episode of the span, inside its duration
    for vid, t in re.findall(r"https://youtu\.be/([\w-]{11})\?t=(\d+)", text):
        assert vid in ("BBBBBBBBBBB", "CCCCCCCCCCC") and int(t) <= 1500
    # episode notes are linked relative to the digest; package headings carry
    # links, so the story links point at the file without a #heading
    assert "[notes](../ep_b/notes.md)" in text
    assert "[E1 notes](../ep_b/notes.md)" in text and "notes.md#" not in text
    # ranking: the story in 2 episodes beats the one in 1
    assert text.index("**Anthropic fund**") < text.index("**Levine pause**")
    assert dg.check_digest_links(text, w43.path.parent, dg.load_collection(col)[1:]) == []


def test_people_are_merged_across_episodes(tmp_path):
    col = _make(tmp_path)
    res = dg.run(col, "week", complete=FakeModel(), as_of=AS_OF)
    text = res[1].path.read_text()
    people = text.split("## People of the week")[1].split("\n## ")[0]
    rows = [ln for ln in people.split("\n") if ln.startswith("| ") and not ln.startswith("| person")]
    names = [r.split("|")[1].strip() for r in rows]
    assert names.count("NLW") == 1 and len(rows) == 2
    levine = next(r for r in rows if "Levine" in r)
    assert "E1, E2" in levine and "Pushed for a pause all week" in levine


def test_new_terms_and_superseded_claims(tmp_path):
    col = _make(tmp_path)
    res = dg.run(col, "week", complete=FakeModel(), as_of=AS_OF)
    text = res[1].path.read_text()
    terms = text.split("## Key terms new this span")[1].split("\n## ")[0]
    assert "`Term ep_b`" in terms and "`Term ep_c`" in terms
    assert "Public Benefit Fund" not in terms          # defined earlier, in week 42
    claims = text.split("## Time-sensitive claims")[1].split("\n## ")[0]
    assert "(superseded) The fund holds 5% (ep_b)" in claims
    assert "superseded later in the span: see [E2 0:13:29]" in claims


def test_idempotent_rerun_and_new_episode_recalls(tmp_path):
    col = _make(tmp_path)
    fake = FakeModel()
    first = dg.run(col, "week", complete=fake, as_of=AS_OF)
    assert sum(r.calls for r in first) == 2 and all(r.written for r in first)
    before = first[1].path.read_bytes()
    again = dg.run(col, "week", complete=fake, as_of=AS_OF)
    assert sum(r.calls for r in again) == 0 and not any(r.written for r in again)
    assert sum(r.cached for r in again) == 2
    assert first[1].path.read_bytes() == before
    # a new episode in week 43 changes that span only
    _make(tmp_path / "more", eps=[("ep_d", "Fund vote", "2026-10-22", "DDDDDDDDDDD")])
    (tmp_path / "more" / "ai_daily_brief" / "ep_d").rename(col / "ep_d")
    third = dg.run(col, "week", complete=fake, as_of=AS_OF)
    assert [r.calls for r in third] == [0, 1]
    assert "E3 = 2026-10-22" in fake.calls[-1][1]


def test_partial_week_is_labelled(tmp_path):
    col = _make(tmp_path)
    res = dg.run(col, "week", complete=FakeModel(), as_of=dt.date(2026, 10, 21))
    text = res[1].path.read_text()
    assert res[1].partial and "partial: true" in text and "Partial week" in text
    assert "partial: false" in res[0].path.read_text()


def test_previous_digest_feeds_what_changed(tmp_path):
    col = _make(tmp_path)
    fake = FakeModel()
    res = dg.run(col, "week", complete=fake, as_of=AS_OF)
    # spans are built oldest first, so W42 exists when W43 is written
    assert "PREVIOUS DIGEST (2026-W42)" not in fake.calls[0][1]
    assert "PREVIOUS DIGEST (2026-W42)" in fake.calls[1][1]
    assert "story: Anthropic fund" in fake.calls[1][1]
    assert "Compared with [2026-W42](2026-W42.md)" in res[1].path.read_text()
    assert "No previous week digest" in res[0].path.read_text()


def test_month_is_built_from_week_digests(tmp_path):
    col = _make(tmp_path)
    fake = FakeModel()
    dg.run(col, "week", complete=fake, as_of=AS_OF)
    dg.run(col, "week", complete=fake, as_of=AS_OF)          # settle the previous-digest chain
    n = len(fake.calls)
    res = dg.run(col, "month", complete=fake, as_of=AS_OF)
    assert len(fake.calls) == n + 1                           # weeks reused, one reduce call
    user = fake.calls[-1][1]
    assert "PARTIAL DIGESTS" in user and "MERGING PARTIAL DIGESTS" in fake.calls[-1][0]
    # week W43's E1/E2 were relabelled to the month's E2/E3
    assert "Development in E3" in user and "E3 0:13:29" in user
    assert res[0].path.name == "2026-10.md" and res[0].link_problems == []


def test_map_reduce_when_over_budget(tmp_path):
    col = _make(tmp_path)
    fake = FakeModel()
    res = dg.run(col, "week", since=dt.date(2026, 10, 19), complete=fake, as_of=AS_OF,
                 max_chars=900)
    assert res[0].calls == 3                                  # 2 partials + 1 reduce
    assert "MERGING PARTIAL DIGESTS" in fake.calls[-1][0]
    assert res[0].link_problems == []


def test_run_dir_collection_with_plain_citations(tmp_path):
    col = tmp_path / "runs"
    run = col / "20261019_x"
    (run / "summaries").mkdir(parents=True)
    (run / "summaries" / "x.summary.md").write_text(_notes(
        "X", "2026-10-19", "XXXXXXXXXXX", [("NLW", "host", "Host", "Hosts", "0:00:10")],
        [("Something", "0:13:29"), ("Other", "0:09:54")], [], [], linked=False))
    (run / "source_meta.json").write_text(json.dumps(
        {"source_id": "XXXXXXXXXXX", "url": "https://www.youtube.com/watch?v=XXXXXXXXXXX",
         "published_at": "2026-10-19", "duration_s": 1500.0}))
    res = dg.run(col, "week", complete=FakeModel(), as_of=AS_OF, out=tmp_path / "digests")
    text = res[0].path.read_text()
    assert res[0].link_problems == [] and "summaries/x.summary.md" in text
    # plain headings get a section anchor in the story's notes link
    assert "x.summary.md#Window%201%200%2000%2000" in text


def test_no_episodes_is_an_error(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(dg.DigestError):
        dg.run(tmp_path / "empty", "week", complete=FakeModel())
