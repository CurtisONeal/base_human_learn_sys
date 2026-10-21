"""annotate-people web lookup (WP-P3): trigger selection, batching, source
budget, provenance, cache/idempotence, privacy, contradictions.
The CLI is mocked; assertions are on what the lookup receives and on the
files a reader gets."""

import datetime as dt
import json
import math
from pathlib import Path

import pytest

from test_annotate_people import FakeLLM, _ep, _name, _scope, _snapshot
from video2mdnotes.core import annotate_people as ap, digest as dg, summarizer

AS_OF = dt.date(2026, 10, 21)


class FakeWeb:
    """Mocked web-lookup CLI. `found`: name/reference -> (name, description,
    sources[, fallback, agrees])."""

    def __init__(self, found=None):
        self.found = found or {}
        self.calls = []
        self.systems = []

    def __call__(self, system, user):
        assert user.startswith("TARGETS:\n")
        targets = json.loads(user.split("TARGETS:\n", 1)[1])
        self.calls.append(targets)
        self.systems.append(system)
        out = []
        for t in targets:
            key = t.get("reference") or t["names"][0]
            hit = self.found.get(key)
            if not hit:
                out.append({"id": t["id"], "status": "not_found", "name": "", "description": "",
                            "sources": [], "confidence": "", "fallback_used": False,
                            "agrees_with_model": None, "note": "no matching page"})
                continue
            name, desc, srcs, *rest = hit
            fb = rest[0] if rest else False
            agrees = rest[1] if len(rest) > 1 else None
            out.append({"id": t["id"], "status": "found", "name": name, "description": desc,
                        "sources": srcs, "confidence": "high", "fallback_used": fb,
                        "agrees_with_model": agrees, "note": ""})
        return json.dumps({"targets": out}), "claude-cli/fake"


def _setup(tmp_path, rows, body="", cutoff="2026-06", unknown=(), private=(), roles=None, day="2026-10-16"):
    vault = tmp_path / "vault" / "subj"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", day, rows, body=body)]
    llm = FakeLLM(cutoff=cutoff, unknown=unknown, private=private, roles=roles or {})
    return vault, paths, llm


ROWS = [("NLW", "host", "Host", "Hosts.", "[0:01:00]"),
        ("Known Person", "quoted", "CEO of Big Co", "Said.", "[0:01:00]"),
        ("Obscure Researcher", "quoted", "a researcher at Lab X", "Said.", "[0:01:00]"),
        ("Jo Listener", "quoted", "a listener who wrote in", "Asked.", "[0:01:00]"),
        ("Pat Winner", "giveaway winner", "not stated", "Won.", "[0:01:00]"),
        ("Unnamed host", "host", "not stated", "-", "[0:01:00]")]
BODY = "- OpenAI's CEO announced the device [0:02:00]\n"
SRC = ["https://openai.com/leadership", "https://en.wikipedia.org/wiki/Sam_Altman_(x)",
       "https://www.reuters.com/a", "https://extra.example/4"]


def test_trigger_selection_only_unknown_and_post_cutoff(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS, body=BODY, unknown={"Obscure Researcher"},
                               private={"Jo Listener"}, roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb()
    ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert len(web.calls) == 1
    sent = web.calls[0]
    kinds = [(t["kind"], t.get("reference") or t["names"][0]) for t in sent]
    # roles first, then people with no model knowledge — nobody else
    assert kinds == [("role", "OpenAI's CEO"), ("person", "Obscure Researcher")]
    assert sent[0]["model_answer"] == {"name": "Sam Altman", "confidence": "high"}
    assert sent[0]["episode_date"] == "2026-10-16"
    flat = json.dumps(sent)
    for never in ("Jo Listener", "Pat Winner", "Unnamed", "Known Person", "NLW"):
        assert never not in flat
    # the system prompt carries the 3-source budget
    assert "at most those 3" in web.systems[0] and "try up\n   to 3 more" in web.systems[0]


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
    assert res.web["people"] == {"eligible": 23, "found": 0, "not_found": 20, "pending": 3}
    # next run: only the 3 not yet looked up, one call
    web2 = FakeWeb()
    res2 = ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web2, as_of=AS_OF)
    assert [len(c) for c in web2.calls] == [3] and res2.web["people"]["pending"] == 0
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


def test_provenance_and_cache_idempotent(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:3], body=BODY, unknown={"Obscure Researcher"},
                               roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb({
        "OpenAI's CEO": ("Sam Altman", "Sam Altman is the CEO of OpenAI. He co-founded it.", SRC),
        "Obscure Researcher": ("Obscure Researcher", "Obscure Researcher is a researcher at Lab X. "
                               "She works on agents.", SRC[2:]),
    })
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert res.web["found"] == 2 and res.web["calls"] == 1
    note = (vault / "people" / "obscure_researcher.md").read_text()
    assert ("## Description — per web lookup 2026-10-21 (sources: [reuters.com](https://www.reuters.com/a), "
            "[extra.example](https://extra.example/4))") in note
    assert "description_source: web" in note and ap.NO_KNOWLEDGE not in note
    idx = (vault / "people" / "_index.md").read_text()
    assert "Obscure Researcher is a researcher at Lab X. _(web lookup)_" in idx
    # role: verify cleared (lookup confirms the model), provenance replaces the label
    notes = paths[0].read_text()
    want = ("OpenAI's CEO (→ [Sam Altman](../people/sam_altman.md), as of 2026-10-16, per web lookup "
            "2026-10-21; sources: [openai.com](https://openai.com/leadership), [en.wikipedia.org]"
            "(https://en.wikipedia.org/wiki/Sam_Altman_%28x%29), [reuters.com](https://www.reuters.com/a))")
    assert want in notes and "per model knowledge" not in notes
    log = (vault / "people" / "_resolutions.md").read_text()
    assert "; confirmed" in log and ap.VERIFY not in log
    assert res.roles[0]["verify"] is False
    # the digest strips the web annotation too
    assert "per web lookup" not in dg.load_episode(paths[0]).body
    # rerun: 0 model calls, 0 web calls, byte-identical
    before = _snapshot(tmp_path)
    web2, llm2 = FakeWeb(), FakeLLM()
    again = ap.run(_scope(tmp_path, paths), llm2, web_complete=web2, as_of=AS_OF)
    assert again.calls == 0 and web2.calls == [] and again.web["calls"] == 0
    assert _snapshot(tmp_path) == before
    # --refresh re-describes but keeps the cached lookup (no new web call)
    web3 = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(unknown={"Obscure Researcher"},
                                            roles={"OpenAI's CEO": "Sam Altman"}),
           web_complete=web3, refresh=True, as_of=AS_OF)
    assert web3.calls == []


def test_stale_model_annotation_is_replaced(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:1], body=BODY, roles={"OpenAI's CEO": "Sam Altman"})
    ap.run(_scope(tmp_path, paths), llm, web=False, web_complete=FakeWeb(), as_of=AS_OF)
    assert "per model knowledge — verify" in paths[0].read_text()
    web = FakeWeb({"OpenAI's CEO": ("Sam Altman", "Sam Altman is the CEO of OpenAI.", SRC[:1])})
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


def test_contradiction_is_noted_not_overwritten(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:1], body=BODY, roles={"OpenAI's CEO": "Sam Altman"})
    web = FakeWeb({"OpenAI's CEO": ("Jane Newceo", "Jane Newceo is the CEO of OpenAI since 2026.",
                                    SRC[:2], False, False)})
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    state = json.loads((tmp_path / "collection" / "people" / ap.STATE_FILE).read_text())
    rec = state["roles"]["openai s ceo|2026-10-16"]
    assert rec["name"] == "Sam Altman" and rec["verify"] is True       # model answer kept
    assert rec["web"]["name"] == "Jane Newceo"
    notes = paths[0].read_text()
    assert "[Jane Newceo](../people/jane_newceo.md)?, as of 2026-10-16, per web lookup 2026-10-21" in notes
    assert "model knowledge: Sam Altman — verify" in notes
    log = (vault / "people" / "_resolutions.md").read_text()
    assert "contradicts model knowledge (Sam Altman) — verify" in log
    assert res.roles[0]["verify"] is True
    person = (vault / "people" / "jane_newceo.md").read_text()
    assert "per web lookup 2026-10-21" in person


def test_not_found_keeps_no_knowledge_and_is_cached(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS[:3], unknown={"Obscure Researcher"})
    web = FakeWeb()
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    note = (vault / "people" / "obscure_researcher.md").read_text()
    assert ap.NO_KNOWLEDGE in note and "_Web lookup 2026-10-21: not found (no matching page)._" in note
    assert res.web["not_found"] == 1
    web2 = FakeWeb()
    ap.run(_scope(tmp_path, paths), FakeLLM(), web_complete=web2, as_of=AS_OF)
    assert web2.calls == []


def test_private_never_looked_up_even_if_unknown(tmp_path):
    vault, paths, llm = _setup(tmp_path, ROWS, unknown={"Jo Listener", "Obscure Researcher"},
                               private={"Jo Listener"})
    web = FakeWeb()
    ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert [t["names"][0] for c in web.calls for t in c] == ["Obscure Researcher"]


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
