"""annotate-people web lookup (WP-P3, updated for WP-P4): trigger selection,
batching, source budget, provenance, cache/idempotence, privacy,
contradictions. The CLI is mocked (kind-aware: role-on-date, quote-first
voice, post-cutoff bio); assertions are on what the lookup receives and on
the files a reader gets. WP-P4 specifics: tests/test_annotate_voices.py."""

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from test_annotate_people import FakeLLM, _ep, _name, _scope, _snapshot
from video2mdnotes.core import annotate_people as ap, digest as dg, summarizer

AS_OF = dt.date(2026, 10, 21)


def role_reply(name, sources=(), changes=(), confidence="high", description=None, status="found"):
    """sources: (url, date, kind, live) tuples."""
    return {"status": status, "name": name,
            "description": description if description is not None else f"{name} is the CEO.",
            "sources": [{"url": u, "date": d, "kind": k, "live": live, "states": "names the CEO"}
                        for u, d, k, live in sources],
            "changes": list(changes), "confidence": confidence, "fallback_used": False, "note": ""}


def voice_reply(tier, handle="", evidence=(), bio=True, topic=True, quote_url="", quote_found=None,
                affiliation="co-founder of an AI startup", atype="founder", description=None,
                fallback=False):
    """evidence: (url, kind, login_wall, supports) tuples."""
    return {"tier": tier, "handle": handle, "display_name": handle.title() if handle else "",
            "real_name": "", "profile_url": f"https://x.com/{handle}" if handle else "",
            "affiliation": affiliation, "affiliation_type": atype, "bio_ai_related": bio,
            "topic_match": topic, "quote_found": bool(quote_url) if quote_found is None else quote_found,
            "quote_url": quote_url,
            "evidence": [{"url": u, "kind": k, "date": "", "login_wall": lw, "supports": list(sup)}
                         for u, k, lw, sup in evidence],
            "description": description if description is not None else
            f"{handle or 'They'} posts about AI agents.", "fallback_used": fallback, "note": ""}


class FakeWeb:
    """Mocked web-lookup CLI, kind-aware.
    roles:  "org|role" -> reply dict (role_reply)
    voices: first name in `names` -> reply dict (voice_reply)
    people: first name -> WP-P3 reply dict
    Anything else: not found / unknown."""

    def __init__(self, roles=None, voices=None, people=None):
        self.roles, self.voices, self.people = roles or {}, voices or {}, people or {}
        self.calls = []
        self.systems = []

    def __call__(self, system, user):
        assert user.startswith("TARGETS:\n")
        targets = json.loads(user.split("TARGETS:\n", 1)[1])
        self.calls.append(targets)
        self.systems.append(system)
        out = []
        for t in targets:
            if t["kind"] == "role":
                hit = self.roles.get(f"{t['org']}|{t['role']}") or role_reply("", status="not_found")
            elif t["kind"] == "voice":
                hit = self.voices.get(t["names"][0]) or voice_reply("unknown")
            else:
                hit = self.people.get(t["names"][0]) or {"status": "not_found", "sources": []}
            out.append({"id": t["id"], **hit})
        return json.dumps({"targets": out}), "claude-cli/fake"

    def kinds(self):
        return [[t["kind"] for t in c] for c in self.calls]


def _setup(tmp_path, rows, body="", cutoff="2026-06", unknown=(), private=(), roles=None, day="2026-10-16"):
    vault = tmp_path / "vault" / "subj"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", day, rows, body=body)]
    llm = FakeLLM(cutoff=cutoff, unknown=unknown, private=private, roles=roles or {})
    return vault, paths, llm


ROWS = [("NLW", "host", "Host", "Hosts.", "[0:01:00]"),
        ("Known Person", "quoted", "CEO of Big Co", "Said.", "[0:01:00]"),
        ("Obscure Researcher", "quoted", "a researcher at Lab X", "Said agents are \"the next big shift\".",
         "[0:01:00]"),
        ("Jo Listener", "quoted", "a listener who wrote in", "Asked.", "[0:01:00]"),
        ("Pat Winner", "giveaway winner", "not stated", "Won.", "[0:01:00]"),
        ("Unnamed host", "host", "not stated", "-", "[0:01:00]")]
BODY = "- OpenAI's CEO announced the device [0:02:00]\n"
SRC = ["https://openai.com/leadership", "https://en.wikipedia.org/wiki/Sam_Altman_(x)",
       "https://www.reuters.com/a", "https://extra.example/4"]
RECENT = role_reply("Sam Altman", [("https://www.reuters.com/a", "2026-09-30", "news", False),
                                   ("https://en.wikipedia.org/wiki/Sam_Altman_(x)", "2026-10-10",
                                    "reference", False)])


def test_trigger_selection_only_unknown_and_post_cutoff(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS, body=BODY, unknown={"Obscure Researcher"},
                               private={"Jo Listener"}, roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb()
    ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    # one call per kind: the role (holder on the date), then the unknown person (voice)
    assert web.kinds() == [["role"], ["voice"]]
    role, voice = web.calls[0][0], web.calls[1][0]
    assert role["question"] == "Who held CEO at OpenAI on 2026-10-16?"
    assert role["last_confirmed"]["name"] == "Sam Altman" and role["date"] == "2026-10-16"
    assert voice["names"][0] == "Obscure Researcher"
    flat = json.dumps(web.calls)
    for never in ("Jo Listener", "Pat Winner", "Unnamed", "Known Person", "NLW"):
        assert never not in flat
    # each kind gets its own prompt
    assert "WHO HELD A ROLE ON A DATE" in web.systems[0]
    assert "two signals must agree" in web.systems[1]


def test_pre_cutoff_role_is_not_looked_up(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:2], body=BODY, cutoff="2026-12",
                               roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert web.calls == [] and res.web["calls"] == 0


def test_batching_and_max_web_calls(tmp_path):
    rows = [(_name(i), "quoted", f"engineer {i}", "Said.", "[0:01:00]") for i in range(23)]
    vault, paths, llm = _setup(tmp_path, rows, unknown={_name(i) for i in range(23)})
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, max_web_calls=2, as_of=AS_OF)
    assert [len(c) for c in web.calls] == [ap.WEB_BATCH, ap.WEB_BATCH] == [10, 10]
    assert res.web["calls"] == 2 and "max-web-calls 2" in res.web["stopped"]
    assert res.web["voices"] == {"eligible": 23, "confirmed": 0, "probable": 0,
                                 "unknown": 20, "pending": 3}
    # next run: only the 3 not yet looked up, one call
    web2 = FakeWeb()
    res2 = ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web2, as_of=AS_OF)
    assert [len(c) for c in web2.calls] == [3] and res2.web["voices"]["pending"] == 0
    assert math.ceil(23 / ap.WEB_BATCH) == 3


def test_source_budget_enforced():
    x = {"status": "found", "name": "A B", "description": "One. Two. Three. Four.",
         "sources": SRC + ["https://more.example/5", "not a url"], "confidence": "high"}
    w = ap.normalize_web(dict(x, fallback_used=False), "person", AS_OF, "m")
    assert w["sources"] == SRC[:3]
    assert w["description"] == "One. Two. Three."
    w = ap.normalize_web(dict(x, fallback_used=True), "person", AS_OF, "m")
    assert len(w["sources"]) == 5 and w["fallback_used"] is True       # ≤6 on fallback
    # "found" with no source is not trusted: never invented
    w = ap.normalize_web(dict(x, sources=[]), "person", AS_OF, "m")
    assert w["status"] == "not_found" and w["description"] == ""
    w = ap.normalize_web(dict(x, name=""), "role", AS_OF, "m")
    assert w["status"] == "not_found"
    # the same budget for role sources and voice evidence
    r = ap.normalize_role_date(role_reply("A B", [(u, "2026-10-01", "news", False) for u in SRC]),
                               dt.date(2026, 10, 16), dt.date(2026, 6, 1), "A B", AS_OF, "m")
    assert [s["url"] for s in r["sources"]] == SRC[:3]
    v = ap.normalize_voice(voice_reply("probable", "ab", [(u, "snippet", True, ["bio", "topic"])
                                                         for u in SRC]), AS_OF, "m")
    assert len(v["evidence"]) == 3


def test_provenance_and_cache_idempotent(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:3], body=BODY, unknown={"Obscure Researcher"},
                               roles={"OpenAI's CEO": "Sam Altman"})
    q = "https://x.com/obscure/status/1"
    web = FakeWeb(roles={"OpenAI|CEO": RECENT}, voices={"Obscure Researcher": voice_reply(
        "confirmed", "obscure", [(q, "post", False, ["quote", "topic"]),
                                 ("https://x.com/obscure", "profile", False, ["bio"])],
        quote_url=q, affiliation="researcher at Lab X", atype="researcher",
        description="Obscure Researcher is a researcher at Lab X. She posts about agents.")})
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert res.web["found"] == 2 and res.web["calls"] == 2
    note = (vault / "people" / "obscure_researcher.md").read_text()
    assert "## Description — per web lookup 2026-10-21 (confirmed identity)" in note
    assert "## Identity — per web lookup 2026-10-21 (confirmed)" in note
    assert "[@obscure](https://x.com/obscure)" in note and "Quoted post: [x.com](" + q + ")" in note
    assert "description_source: voice" in note and ap.NO_KNOWLEDGE not in note
    idx = (vault / "people" / "_index.md").read_text()
    assert "Obscure Researcher is a researcher at Lab X. _(web lookup, confirmed)_" in idx
    # role: verify cleared (a source dated within 90 days), dated provenance in the notes
    notes = paths[0].read_text()
    want = ("OpenAI's CEO (→ [Sam Altman](../people/sam_altman.md), as of 2026-10-16, per web lookup "
            "2026-10-21; confirmed by a source dated 2026-10-10; sources: [reuters.com 2026-09-30]"
            "(https://www.reuters.com/a), [en.wikipedia.org 2026-10-10]"
            "(https://en.wikipedia.org/wiki/Sam_Altman_%28x%29))")
    assert want in notes and "per model knowledge" not in notes
    log = (vault / "people" / "_resolutions.md").read_text()
    assert "confirmed by a source dated 2026-10-10" in log and ap.VERIFY not in log
    assert res.roles[0]["verify"] is False
    # the digest strips the web annotation too
    assert "per web lookup" not in dg.load_episode(paths[0]).body
    # rerun: 0 model calls, 0 web calls, byte-identical
    before = _snapshot(tmp_path)
    web2, llm2 = FakeWeb(), FakeLLM()
    again = ap.run(_scope(tmp_path, paths), llm2, web_complete=web2, as_of=AS_OF)
    assert again.calls == 0 and web2.calls == [] and again.web["calls"] == 0
    assert _snapshot(tmp_path) == before
    # --refresh re-describes but keeps the cached lookups (no new web call)
    web3 = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(unknown={"Obscure Researcher"},
                                            roles={"OpenAI's CEO": "Sam Altman"}),
           web_complete=web3, refresh=True, as_of=AS_OF)
    assert web3.calls == []


def test_stale_model_annotation_is_replaced(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:1], body=BODY, roles={"OpenAI's CEO": "Sam Altman"})
    ap.run(_scope(tmp_path, paths), llm, web=False, web_complete=FakeWeb(), as_of=AS_OF)
    assert "per model knowledge — verify" in paths[0].read_text()
    web = FakeWeb(roles={"OpenAI|CEO": RECENT})
    ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web, as_of=AS_OF)
    text = paths[0].read_text()
    assert text.count("(→ ") == 1 and "per web lookup 2026-10-21" in text and "verify" not in text


def test_no_web_flag_disables(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:3], body=BODY, unknown={"Obscure Researcher"},
                               roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), llm, web=False, web_complete=web, as_of=AS_OF)
    assert web.calls == [] and res.web["calls"] == 0
    assert ap.NO_KNOWLEDGE in (vault / "people" / "obscure_researcher.md").read_text()


def test_contradiction_without_change_is_noted_not_overwritten(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:1], body=BODY, roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb(roles={"OpenAI|CEO": role_reply(
        "Jane Newceo", [("https://www.reuters.com/a", "2026-10-01", "news", False)],
        description="Jane Newceo is the CEO of OpenAI.")})
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    state = json.loads((tmp_path / "collection" / "people" / ap.STATE_FILE).read_text())
    rec = state["roles"]["openai s ceo|2026-10-16"]
    assert rec["name"] == "Sam Altman" and rec["verify"] is True       # model answer kept
    assert rec["role_check"]["name"] == "Jane Newceo"
    notes = paths[0].read_text()
    assert "[Jane Newceo](../people/jane_newceo.md)?, as of 2026-10-16, per web lookup 2026-10-21" in notes
    assert "model knowledge: Sam Altman — verify" in notes
    log = (vault / "people" / "_resolutions.md").read_text()
    assert "contradicts model knowledge (Sam Altman) — verify" in log
    assert res.roles[0]["verify"] is True
    person = (vault / "people" / "jane_newceo.md").read_text()
    assert "per web lookup 2026-10-21" in person


def test_unknown_voice_keeps_no_knowledge_and_is_cached(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:3], unknown={"Obscure Researcher"})
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    note = (vault / "people" / "obscure_researcher.md").read_text()
    assert ap.NO_KNOWLEDGE in note and "_Quote-first web lookup 2026-10-21: unknown" in note
    assert res.web["not_found"] == 1 and res.web["voices"]["unknown"] == 1
    web2 = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web2, as_of=AS_OF)
    assert web2.calls == []


def test_wp_p3_low_confidence_match_still_not_adopted(tmp_path):
    """A cached WP-P3 low-confidence match (state from before WP-P4) is shown,
    not adopted, and the person is still eligible for the voice lookup."""
    vault, paths, llm = _setup(tmp_path, ROWS[:3], unknown={"Obscure Researcher"})
    ap.run(_scope(tmp_path, paths), llm, web=False, as_of=AS_OF)
    sp = tmp_path / "collection" / "people" / ap.STATE_FILE
    state = json.loads(sp.read_text())
    state["people"]["obscure_researcher"]["web"] = {
        "status": "found", "name": "Obscure Researcha", "description": "Obscure Researcha builds tools.",
        "sources": SRC[:1], "confidence": "low", "fallback_used": False, "agrees_with_model": None,
        "note": "", "looked_up_at": "2026-10-20", "model": "m"}
    sp.write_text(json.dumps(state))
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web, as_of=AS_OF)
    assert web.kinds() == [["voice"]]
    note = (vault / "people" / "obscure_researcher.md").read_text()
    assert ap.NO_KNOWLEDGE in note and "description_source: none" in note
    assert "low confidence, not adopted: Obscure Researcha builds tools._" in note
    assert "Probable spelling" not in note
    assert res.web["people"]["low_confidence"] == 1 and res.web["people"]["found"] == 0


def test_private_never_looked_up_even_if_unknown(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS, unknown={"Jo Listener", "Obscure Researcher"},
                               private={"Jo Listener"})
    web = FakeWeb()
    ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert [t["names"][0] for c in web.calls for t in c] == ["Obscure Researcher"]
    assert "Jo Listener" not in json.dumps(web.calls)


def test_web_cli_argv_subscription_web_tools_only(monkeypatch, tmp_path):
    argv = summarizer.web_lookup_argv("claude", "opus", Path("/x/sys.txt"))
    assert argv[:3] == ["claude", "-p", "--model"]
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"
    assert argv[argv.index("--allowedTools") + 1] == "WebSearch,WebFetch"
    dis = argv[argv.index("--disallowedTools") + 1].split(",")
    assert "Bash" in dis and "Read" in dis and "WebSearch" not in dis
    assert "--strict-mcp-config" in argv
    # metered entries are never used for a web lookup
    seen = []

    def fake_cli(model, prompt_file, user):
        seen.append(model)
        raise RuntimeError("boom")
    monkeypatch.setattr(summarizer, "_complete_claude_cli_web", fake_cli)
    monkeypatch.setattr(summarizer.settings, "llm_models",
                        ["openai/gpt-4o", "claude-cli/opus", "claude-cli/sonnet"])
    with pytest.raises(RuntimeError, match="no subscription"):
        summarizer.complete_web_lookup("sys", "user")
    assert seen == ["claude-cli/opus", "claude-cli/sonnet"]
