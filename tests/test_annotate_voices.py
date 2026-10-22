"""WP-P4: quote-first identity lookup for quoted people (tiers), the "AI
voices" ranking in sources/_index.md, and role-on-date checks with dated
evidence + a change check. CLI mocked; assertions on what the lookup receives
(queries, question) and on what the reader gets."""

import datetime as dt
import json
import math

from test_annotate_people import FakeLLM, _ep, _name, _scope, _snapshot
from test_annotate_web import AS_OF, FakeWeb, role_reply, voice_reply
from video2mdnotes.core import annotate_people as ap, digest as dg

EP = dt.date(2026, 10, 16)
LAST = dt.date(2026, 6, 1)                     # model knowledge cutoff 2026-06


# --------------------------------------------------------------------------- #
# A. quote-first queries
# --------------------------------------------------------------------------- #

def test_quote_first_query_building(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [("Kyo (ASR spelling)", "quoted", "not stated",
             "Calls it a huge jump and good value, but \"I don't feel like I can trust it\".", "[0:22:48]"),
            ("Bob Seo", "quoted", "founder of AI for Finance",
             "Says banks are \"right about the risk, wrong about the fix\": agents locked down.", "[0:07:42]"),
            ("B on X", "quoted", "not stated", "Built a procedurally generated 3D helicopter in Three.js.",
             "[0:21:21]")]
    body = "- Kyo says he does not trust it [0:22:48].\n"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows, body=body)]
    web = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(unknown={"Kyo", "Bob Seo", "B on X"}),
           web_complete=web, as_of=AS_OF)
    sent = {t["names"][0]: t for t in web.calls[0]}
    kyo = sent["Kyo"]
    # the exact quote first, with the 7-day window before the episode, on x.com
    assert kyo["exact_quotes"] == ["I don't feel like I can trust it"]
    assert kyo["queries"][0] == 'site:x.com "I don\'t feel like I can trust it" after:2026-10-07 before:2026-10-15'
    assert kyo["queries"][1].startswith("site:x.com huge jump good value")         # then the claim words
    assert "x.com/kyo" in kyo["queries"]                                            # then the handle
    assert kyo["queries"][-1] == '"Kyo" Episode ep1 AI'                              # the name last
    assert kyo["episodes"][0]["window"] == ["2026-10-07", "2026-10-14"]
    assert kyo["episodes"][0]["topic"] == "Episode ep1"
    assert "Kyo says he does not trust it." in kyo["episodes"][0]["context"]
    # a multi-word name: quote first, name in the claim query, no handle guess
    bob = sent["Bob Seo"]
    assert bob["queries"][0].startswith('site:x.com "right about the risk, wrong about the fix"')
    assert bob["handle_candidates"] == [] and bob["queries"][1].startswith("site:x.com Bob Seo ")
    # a spoken handle without a quote: claim query first; "B" is too short for a handle
    b = sent["B on X"]
    assert b["exact_quotes"] == [] and b["queries"][0].startswith("site:x.com ")
    assert "procedurally" in b["queries"][0]
    assert not any(q.startswith('site:x.com "') for q in b["queries"])


def test_handle_candidates():
    assert ap.handle_candidates(["Pleometric"]) == ["pleometric"]
    assert ap.handle_candidates(["Faw for AI"]) == ["fawforai"]
    assert ap.handle_candidates(["Simon Smith"]) == []
    assert ap.handle_candidates(["Max on X"]) == ["max"]


# --------------------------------------------------------------------------- #
# A. two-signal acceptance + tiers
# --------------------------------------------------------------------------- #

POST = "https://x.com/kyo/status/1"
PROFILE = "https://x.com/kyo"


def _v(reply):
    return ap.normalize_voice(reply, AS_OF, "m")


def test_two_signal_acceptance_and_tiers():
    ev_quote = [(POST, "post", False, ["quote", "topic"]), (PROFILE, "profile", False, ["bio"])]
    v = _v(voice_reply("confirmed", "kyo", ev_quote, quote_url=POST))
    assert v["tier"] == "confirmed" and v["quote_url"] == POST and v["handle"] == "kyo"
    # AI bio but no post on the topic -> unknown
    v = _v(voice_reply("probable", "kyo", [(PROFILE, "profile", False, ["bio"])], topic=False))
    assert v["tier"] == "unknown" and v["handle"] == "" and v["evidence"] == []
    # topic but the bio is not AI-related -> unknown
    v = _v(voice_reply("probable", "kyo", [(POST, "post", False, ["topic"]),
                                           (PROFILE, "profile", False, ["bio"])], bio=False))
    assert v["tier"] == "unknown"
    # the model claims a bio signal no evidence item supports -> unknown
    v = _v(voice_reply("probable", "kyo", [(POST, "post", False, ["topic"])]))
    assert v["tier"] == "unknown" and "downgraded from probable" in v["note"]
    # no handle / profile -> unknown, whatever the model says
    v = _v(voice_reply("confirmed", "", ev_quote, quote_url=POST))
    assert v["tier"] == "unknown"
    # "confirmed" claimed but the quote URL is not among the evidence -> probable
    v = _v(voice_reply("confirmed", "kyo", [(PROFILE, "profile", False, ["bio", "topic"])],
                       quote_url="https://x.com/kyo/status/999"))
    assert v["tier"] == "probable" and v["quote_url"] == ""
    assert "downgraded from confirmed: quote not seen in the evidence" in v["note"]
    # the model's own tier is a ceiling: never upgraded
    v = _v(voice_reply("probable", "kyo", ev_quote, quote_url=POST))
    assert v["tier"] == "probable"
    # affiliation type: given, or classified from the affiliation text
    v = _v(voice_reply("confirmed", "kyo", ev_quote, quote_url=POST, affiliation="partner at a VC fund",
                       atype="?"))
    assert v["affiliation_type"] == "investor"


def test_x_login_wall_fallback_is_probable(tmp_path):
    """x.com pages behind the login: snippets count as evidence, but without
    the quote itself never more than probable."""
    snip = [("https://www.google.com/search?q=kyo", "snippet", True, ["bio", "topic"]),
            (PROFILE, "profile", True, ["bio"])]
    v = _v(voice_reply("confirmed", "kyo", snip, quote_found=True, quote_url=""))
    assert v["tier"] == "probable" and v["login_wall_only"] is True
    # ... unless the snippet shows the quote itself
    snip_q = [(POST, "snippet", True, ["quote", "bio"])]
    v = _v(voice_reply("confirmed", "kyo", snip_q, quote_url=POST))
    assert v["tier"] == "confirmed"
    # end to end: the note says probable, behind the login, and does not adopt a bio
    vault = tmp_path / "vault" / "subj"
    rows = [("Kyo", "quoted", "not stated", "Calls it \"a huge jump in quality\".", "[0:22:48]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows)]
    web = FakeWeb(voices={"Kyo": voice_reply("confirmed", "kyo", snip, quote_found=True,
                                              affiliation="indie AI developer")})
    res = ap.run(_scope(tmp_path, paths), FakeLLM(unknown={"Kyo"}), web_complete=web, as_of=AS_OF)
    note = (vault / "people" / "kyo.md").read_text()
    assert "## Identity — per web lookup 2026-10-21 (probable)" in note
    assert "(x.com login wall: search snippets only)" in note
    assert "[x.com (profile, login wall)](https://x.com/kyo)" in note
    assert ap.NO_KNOWLEDGE in note and "description_source: none" in note
    idx = (vault / "people" / "_index.md").read_text()
    assert "Probable: [@kyo](https://x.com/kyo) — indie AI developer _(web lookup, probable)_" in idx
    assert res.web["voices"]["probable"] == 1


# --------------------------------------------------------------------------- #
# A. AI voices: scoring + ranking
# --------------------------------------------------------------------------- #

def test_signal_score_formula():
    assert ap.signal_score(3, 1, 0, "founder") == round(3 * 1 * 1 * 1.3, 2)
    assert ap.signal_score(2, 3, 30, "lab") == round(2 * (1 + math.log(3)) * 0.5 * 1.5, 2)
    assert ap.signal_score(1, 1, 0, "unknown-type") == 1.0
    assert ap.classify_affiliation("research scientist at Google DeepMind") == "lab"
    assert ap.classify_affiliation("professor at Wharton") == "researcher"
    assert ap.classify_affiliation("reporter at The Information") == "journalist"


def test_ai_voices_ranked_table(tmp_path):
    vault = tmp_path / "vault" / "subj"
    r1 = [("NLW", "host", "Host of the AI show", "Hosts.", "[0:01:00]"),
          ("Ada Labperson", "quoted", "researcher at OpenAI", "Said agents work.", "[0:02:00] [0:02:30]"),
          ("Kyo", "quoted", "not stated", "Calls it \"a huge jump in quality\".", "[0:03:00]"),
          ("Bo Banker", "quoted", "CEO of a bank", "Said rates matter.", "[0:04:00]"),
          ("Zed Unknown", "quoted", "not stated", "Said something.", "[0:05:00]"),
          ("Discussed Only", "person discussed", "AI founder", "Was discussed.", "[0:06:00]")]
    r2 = [("Ada Labperson", "quoted", "researcher at OpenAI", "Said more.", "[0:01:00]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", r1),
             _ep(vault, "ep2", "BBBBBBBBBBB", "2026-10-20", r2)]

    llm = FakeLLM(unknown={"Kyo", "Zed Unknown"})
    post = "https://x.com/kyo/status/7"
    web = FakeWeb(voices={"Kyo": voice_reply("confirmed", "kyo", [
        (post, "post", False, ["quote"]), ("https://x.com/kyo", "profile", False, ["bio"])],
        quote_url=post, affiliation="founder of an AI startup", atype="founder")})
    ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    # the fake model's descriptions are not AI-related; make Ada's AI-related via a rerun state edit
    sp = tmp_path / "collection" / "people" / ap.STATE_FILE
    state = json.loads(sp.read_text())
    state["people"]["ada_labperson"]["description"] = "Ada Labperson is a research scientist at OpenAI."
    sp.write_text(json.dumps(state))
    ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=FakeWeb(), as_of=AS_OF)
    for root in (tmp_path / "collection", vault):
        s = (root / "sources" / "_index.md").read_text()
        sec = s.split("## AI voices", 1)[1].split("\n## ", 1)[0]
        assert "signal score = times quoted × episode breadth × recency × affiliation weight" in sec
        rows = [ln for ln in sec.splitlines() if ln.startswith("| ") and not ln.startswith("| #")]
        # Ada: 3 quotes, 2 episodes, last 2026-10-20 (1 day), lab -> ranks above Kyo (1 quote, 7 days)
        assert rows[0].startswith("| 1 | [Ada Labperson](../people/ada_labperson.md) |")
        ada = ap.signal_score(3, 2, 1, "lab")
        assert f"| {ada:.2f} |" in rows[0] and "known (model knowledge)" in rows[0]
        assert "| 3 | 2 | 2026-10-14 | 2026-10-20 | 1 |" in rows[0]
        assert "[0:02:00](https://youtu.be/AAAAAAAAAAA?t=120)" in rows[0]
        assert rows[1].startswith("| 2 | [Kyo](../people/kyo.md) · [@kyo](https://x.com/kyo) |")
        assert "| confirmed |" in rows[1] and f"[x.com]({post})" in rows[1]
        assert len(rows) == 2
        # host, non-AI, discussed-only are not ranked; the unidentified are listed
        assert "NLW" not in sec and "Bo Banker" not in sec and "Discussed Only" not in sec
        assert "quoted but unidentified (tier unknown), 1: Zed Unknown" in sec
        assert "1 identified quoted people whose description is not AI-related" in sec
    assert not ap._check_links(s, tmp_path / "vault" / "subj" / "sources")


# --------------------------------------------------------------------------- #
# B. role on date
# --------------------------------------------------------------------------- #

def _r(reply, date=EP, as_of=AS_OF):
    return ap.normalize_role_date(reply, date, LAST, "Sam Altman", as_of, "m")


def test_role_question_is_only_the_date_question(tmp_path):
    assert ap.role_parts("OpenAI's CEO") == ("CEO", "OpenAI")
    assert ap.role_parts("the Secretary of State") == ("Secretary of State", "")
    assert ap.role_parts("the UK Prime Minister") == ("Prime Minister", "UK")
    vault = tmp_path / "vault" / "subj"
    body = "- The firing of OpenAI's CEO in 2023 still echoes [0:02:00]\n"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", [("NLW", "host", "Host", "-", "[0:01:00]")],
                 body=body)]
    web = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(roles={"OpenAI's CEO": "Sam Altman"}), web_complete=web,
           as_of=AS_OF)
    t = web.calls[0][0]
    assert t["question"] == "Who held CEO at OpenAI on 2026-10-16?"
    assert t["evidence_window"] == ["2026-07-18", "2026-10-16"]
    assert t["change_window"] == ["2026-06-01", "2026-10-16"]
    assert t["last_confirmed"] == {"name": "Sam Altman", "as_of": "2026-06",
                                   "basis": "model knowledge (cutoff)"}
    assert "firing" in t["context_sentence"] and "not the question" in t["context_note"]
    assert "never answer\n  the sentence, answer the `question`" in web.systems[0]


def test_role_dated_evidence_acceptance_and_rejection():
    # a source dated within 90 days before the episode -> confirmed
    r = _r(role_reply("Sam Altman", [("https://reuters.com/a", "2026-08-01", "news", False)]))
    assert r["status"] == "confirmed" and r["basis"] == "a source dated 2026-08-01"
    # only 2023 news about the ousting -> last confirmed 2023-11-18, not confirmed
    old = role_reply("Sam Altman", [("https://aljazeera.com/2023", "2023-11-18", "news", False),
                                    ("https://wfsb.com/2023", "2023-11-18", "news", False)])
    r = _r(old)
    assert r["status"] == "last_confirmed" and r["evidence_date"] == "2023-11-18" and r["basis"] == ""
    res = ap.apply_web_role({"name": "Sam Altman", "verify": True, "confidence": "high", "role_check": r})
    assert res["verify"] is True and res["name"] == "Sam Altman"
    # dated after the episode does not count either; undated non-official neither
    r = _r(role_reply("Sam Altman", [("https://x.example/a", "2026-10-19", "news", False),
                                     ("https://y.example/b", "", "reference", False)]))
    assert r["status"] == "last_confirmed"
    # a live official leadership page -> confirmed (episode ≤90 days old) ...
    live = role_reply("Sam Altman", [("https://openai.com/leadership", "", "official_leadership_page", True)])
    r = _r(live)
    assert r["status"] == "confirmed" and r["basis"].startswith("the live official leadership page")
    # ... but not for an old episode (a live page shows today's holder)
    r = _r(live, date=dt.date(2025, 1, 10))
    assert r["status"] == "last_confirmed"
    # a not-live "official" page is just undated
    r = _r(role_reply("Sam Altman", [("https://openai.com/leadership", "", "official_leadership_page", False)]))
    assert r["status"] == "last_confirmed"
    # not found
    assert _r(role_reply("", status="not_found"))["status"] == "not_found"


def test_change_check_overrides_last_confirmed(tmp_path):
    change = {"date": "2026-08-01", "event": "appointed", "new_holder": "Jane Newceo",
              "previous_holder": "Sam Altman", "url": "https://reuters.com/change"}
    reply = role_reply("Sam Altman", [("https://aljazeera.com/2023", "2023-11-18", "news", False)],
                       changes=[change], description="Jane Newceo is the CEO of OpenAI.")
    r = _r(reply)
    assert r["name"] == "Jane Newceo" and r["change"]["previous_holder"] == "Sam Altman"
    assert r["status"] == "confirmed" and r["basis"] == "a change dated 2026-08-01"
    assert "answer named Sam Altman; the change found wins" in r["note"]
    # a "change" before the last confirmation, or after the episode, is ignored
    for d in ("2026-05-01", "2026-10-20"):
        r2 = _r(role_reply("Sam Altman", [("https://reuters.com/a", "2026-09-01", "news", False)],
                           changes=[dict(change, date=d)]))
        assert r2["name"] == "Sam Altman" and r2["change"] is None
    # end to end: the change wins, both names recorded, not a contradiction
    vault = tmp_path / "vault" / "subj"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", [("NLW", "host", "Host", "-", "[0:01:00]")],
                 body="- OpenAI's CEO announced the device [0:02:00]\n")]
    web = FakeWeb(roles={"OpenAI|CEO": reply})
    res = ap.run(_scope(tmp_path, paths), FakeLLM(roles={"OpenAI's CEO": "Sam Altman"}),
                 web_complete=web, as_of=AS_OF)
    notes = paths[0].read_text()
    assert ("OpenAI's CEO (→ [Jane Newceo](../people/jane_newceo.md), as of 2026-10-16, per web lookup "
            "2026-10-21; changed 2026-08-01: Jane Newceo (appointed; previously Sam Altman); confirmed by "
            "a change dated 2026-08-01; sources: [aljazeera.com 2023-11-18](https://aljazeera.com/2023), "
            "[reuters.com 2026-08-01](https://reuters.com/change))") in notes
    assert "contradicts" not in notes and res.roles[0]["verify"] is False
    assert "per web lookup" not in dg.load_episode(paths[0]).body          # digests strip it


def test_wp_p3_undated_role_lookup_is_rechecked(tmp_path):
    """State from WP-P3 (an undated rec["web"] that 'confirmed' the holder):
    verify is NOT cleared by it, and the role is re-checked for the date once."""
    vault = tmp_path / "vault" / "subj"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", [("NLW", "host", "Host", "-", "[0:01:00]")],
                 body="- OpenAI's CEO announced the device [0:02:00]\n")]
    ap.run(_scope(tmp_path, paths), FakeLLM(roles={"OpenAI's CEO": "Sam Altman"}), web=False, as_of=AS_OF)
    sp = tmp_path / "collection" / "people" / ap.STATE_FILE
    state = json.loads(sp.read_text())
    state["roles"]["openai s ceo|2026-10-16"]["web"] = {
        "status": "found", "name": "Sam Altman", "description": "Sam Altman is the CEO of OpenAI.",
        "sources": ["https://aljazeera.com/2023"], "confidence": "high", "fallback_used": False,
        "agrees_with_model": True, "note": "refers to the November 2023 ousting",
        "looked_up_at": "2026-10-21", "model": "m"}
    sp.write_text(json.dumps(state))
    res = ap.run(_scope(tmp_path, paths), FakeLLM(), web=False, as_of=AS_OF)
    assert res.roles[0]["verify"] is True
    assert "undated sources" in paths[0].read_text()
    web = FakeWeb(roles={"OpenAI|CEO": role_reply(
        "Sam Altman", [("https://reuters.com/a", "2026-10-02", "news", False)])})
    res = ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web, as_of=AS_OF)
    assert web.kinds() == [["role"]] and res.roles[0]["verify"] is False
    state = json.loads(sp.read_text())
    rec = state["roles"]["openai s ceo|2026-10-16"]
    assert rec["web"]["note"] == "refers to the November 2023 ousting"   # history kept
    assert rec["role_check"]["status"] == "confirmed"


# --------------------------------------------------------------------------- #
# C. cache / idempotence, ≤5 web calls, privacy
# --------------------------------------------------------------------------- #

def test_cache_idempotent_and_web_call_cap(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [(_name(i), "quoted", "not stated", f"Said thing {i}.", "[0:01:00]") for i in range(60)]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", rows,
                 body="- OpenAI's CEO announced the device [0:02:00]\n")]
    web = FakeWeb(roles={"OpenAI|CEO": role_reply(
        "Sam Altman", [("https://reuters.com/a", "2026-10-02", "news", False)])})
    res = ap.run(_scope(tmp_path, paths),
                 FakeLLM(unknown={_name(i) for i in range(60)}, roles={"OpenAI's CEO": "Sam Altman"}),
                 web_complete=web, as_of=AS_OF)               # default cap
    assert ap.MAX_WEB_CALLS == 5
    assert len(web.calls) == 5 and web.kinds()[0] == ["role"]
    assert res.web["calls"] == 5 and "max-web-calls 5" in res.web["stopped"]
    assert res.web["voices"]["pending"] == 60 - 4 * ap.WEB_BATCH
    # finished batches are cached: the rerun sends only the rest, never a cached one
    web2 = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web2, as_of=AS_OF)
    sent2 = [t["names"][0] for c in web2.calls for t in c]
    assert len(sent2) == 20 and len(web2.calls) == 2
    first = {t["names"][0] for c in web.calls[1:] for t in c}
    assert not (set(sent2) & first)
    # nothing left: 0 calls, byte-identical
    before = _snapshot(tmp_path)
    web3 = FakeWeb()
    again = ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web3, as_of=AS_OF)
    assert web3.calls == [] and again.web["calls"] == 0 and _snapshot(tmp_path) == before


def test_private_and_unnamed_never_looked_up(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [("Jo Listener", "quoted", "a listener who wrote in", "Said \"this is great stuff\".", "[0:01:00]"),
            ("Pat Winner", "giveaway winner", "not stated", "Won.", "[0:01:00]"),
            ("Unnamed speaker", "quoted", "not stated", "-", "[0:01:00]"),
            ("Kyo", "quoted", "not stated", "Said things.", "[0:01:00]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", rows)]
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), FakeLLM(unknown={"Jo Listener", "Kyo"}, private={"Jo Listener"}),
                 web_complete=web, as_of=AS_OF)
    flat = json.dumps(web.calls)
    assert [t["names"][0] for c in web.calls for t in c] == ["Kyo"]
    for never in ("Jo Listener", "this is great stuff", "Pat Winner", "Unnamed"):
        assert never not in flat
    s = (vault / "sources" / "_index.md").read_text()
    assert "Jo Listener" not in s
    assert res.web["voices"]["eligible"] == 1
