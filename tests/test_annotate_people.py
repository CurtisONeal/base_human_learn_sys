"""annotate-people (WP-P2): person index, vague-role resolution, sources index.
Mocked LLM; asserts on the files a reader gets and on what the model receives."""

import hashlib
import json
import math
from pathlib import Path

import pytest

from video2mdnotes.core import annotate_people as ap, digest as dg

PEOPLE_HEAD = ("| person | role | affiliation & background (as stated) | relevance | citations |\n"
               "|---|---|---|---|---|\n")
SRC_HEAD = ("| source | type | cited by | cited for | date / edition | citations |\n"
            "|---|---|---|---|---|---|\n")


def _ep(root: Path, slug: str, yid: str, day: str, people_rows, body="", sources=()):
    d = root / slug
    d.mkdir(parents=True)
    (d / "source_meta.json").write_text(json.dumps(
        {"url": f"https://youtu.be/{yid}", "published_at": day, "duration_s": 1500}))
    rows = "\n".join("| " + " | ".join(r) + " |" for r in people_rows)
    srcs = "\n".join("| " + " | ".join(r) + " |" for r in sources)
    (d / "notes.md").write_text(
        f"---\ntitle: \"Episode {slug}\"\npublished: {day}\nsource: https://youtu.be/{yid}\n"
        f"published_by: \"The Show\"\nduration_s: 1500\n---\n# Episode {slug}\n\n"
        f"## People\n\n{PEOPLE_HEAD}{rows}\n\n"
        + (f"## Sources Referenced\n\n{SRC_HEAD}{srcs}\n\n" if sources else "")
        + f"## Window 1 [0:00–0:20]\n\n### Summary\n\n- Opening point [0:01:00]\n{body}\n")
    return d / "notes.md"


class FakeLLM:
    def __init__(self, cutoff="2026-06", private=(), unknown=(), roles=None):
        self.calls = []
        self.cutoff, self.private, self.unknown = cutoff, set(private), set(unknown)
        self.roles = roles or {}

    def __call__(self, system, user):
        people = json.loads(user.split("PEOPLE:\n", 1)[1].split("\n\nROLES:\n", 1)[0])
        roles = json.loads(user.split("\n\nROLES:\n", 1)[1])
        self.calls.append((people, roles))
        out = {"knowledge_cutoff": self.cutoff, "people": [], "roles": []}
        for p in people:
            name = p["names"][0]
            known = name not in self.unknown
            out["people"].append({"id": p["id"], "known": known, "private": name in self.private,
                                  "description": f"{name} is a public figure. Known for things." if known else ""})
        for r in roles:
            name = self.roles.get(r["reference"], "")
            out["roles"].append({"id": r["id"], "name": name, "confidence": "high",
                                 "after_cutoff": False, "description": f"{name} is an official."})
        return json.dumps(out), "fake/model"


def _scope(tmp_path, paths):
    eps = ap._episodes_from(paths)
    for i, e in enumerate(eps, 1):
        e.label = f"E{i}"
    roots = [tmp_path / "collection", tmp_path / "vault" / "subj"]
    return ap.Scope(eps, [], roots, "The Show")


def _name(i: int) -> str:
    """Distinct first names, so no ASR sounds-alike merge applies."""
    return f"Q{chr(97 + i // 26)}{chr(97 + i % 26)}zed Example"


def _snapshot(root: Path):
    return {str(p): hashlib.md5(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def test_batching_and_no_redescribe_and_idempotent(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [(_name(i), "quoted", f"CEO of Co{i}", "Said things.", "[0:01:00]")
            for i in range(45)]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows[:30]),
             _ep(vault, "ep2", "BBBBBBBBBBB", "2026-10-15", rows[25:])]
    llm = FakeLLM()
    res = ap.run(_scope(tmp_path, paths), llm, as_of=__import__("datetime").date(2026, 10, 21))
    assert res.calls == math.ceil(45 / ap.BATCH) == 3
    assert [len(p) for p, _ in llm.calls] == [20, 20, 5]
    assert res.people == 45 and not res.link_problems
    for root in (tmp_path / "collection", vault):
        assert (root / "people" / "_index.md").exists()
        note = (root / "people" / "qbazed_example.md").read_text()       # _name(26)
        assert "appearances: 2" in note and "## Description — per model knowledge (cutoff uncertain)" in note
        assert "CEO of Co26 — [Episode ep1](../" in note
    before = _snapshot(tmp_path)
    # rerun: nothing new -> 0 calls, byte-identical
    llm2 = FakeLLM()
    again = ap.run(_scope(tmp_path, paths), llm2, as_of=__import__("datetime").date(2026, 10, 21))
    assert again.calls == 0 and llm2.calls == [] and _snapshot(tmp_path) == before
    # a new person: exactly one call carrying only that person
    paths.append(_ep(vault, "ep3", "CCCCCCCCCCC", "2026-10-16",
                     [(_name(3), "quoted", "x", "y", "[0:01:00]"),
                      ("Brand Newperson", "guest", "z", "w", "[0:01:00]")]))
    llm3 = FakeLLM()
    third = ap.run(_scope(tmp_path, paths), llm3, as_of=__import__("datetime").date(2026, 10, 21))
    assert third.calls == 1 and [p["names"][0] for p in llm3.calls[0][0]] == ["Brand Newperson"]
    # --refresh re-describes everyone
    llm4 = FakeLLM()
    assert ap.run(_scope(tmp_path, paths), llm4, refresh=True).calls == math.ceil(46 / ap.BATCH)


def test_max_calls_leaves_pending_then_resumes(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [(_name(i), "quoted", "x", "y", "[0:01:00]") for i in range(25)]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows)]
    res = ap.run(_scope(tmp_path, paths), FakeLLM(), max_calls=1)
    assert res.calls == 1 and "max-calls" in res.stopped
    pending = [p for p in (vault / "people").glob("q*_example.md") if ap.PENDING in p.read_text()]
    assert len(pending) == 5
    assert ap.run(_scope(tmp_path, paths), FakeLLM()).calls == 1
    assert not any(ap.PENDING in p.read_text() for p in (vault / "people").glob("q*_example.md"))


def test_privacy_and_unknown_people(tmp_path):
    vault = tmp_path / "vault" / "subj"
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", [
        ("NLW (per source description)", "host", "Host of The Show", "Hosts.", "[0:01:00]"),
        ("Pat Winner", "giveaway winner", "not stated", "Won.", "[0:01:00]"),
        ("Jo Listener", "quoted", "a listener who wrote in", "Asked.", "[0:01:00]"),
        ("Obscure Researcher", "quoted", "a researcher", "Said.", "[0:01:00]"),
        ("Unnamed host", "host", "not stated", "-", "[0:01:00]")])]
    llm = FakeLLM(private={"Jo Listener"}, unknown={"Obscure Researcher"})
    res = ap.run(_scope(tmp_path, paths), llm)
    sent = [p["names"][0] for p in llm.calls[0][0]]
    assert "Pat Winner" not in sent and "Unnamed host" not in sent      # never sent
    files = {p.name for p in (vault / "people").glob("*.md")}
    assert "pat_winner.md" not in files and "jo_listener.md" not in files
    assert "nlw.md" in files
    index = (vault / "people" / "_index.md").read_text()
    assert "Pat Winner" not in index and "Jo Listener" not in index
    assert res.skipped_private == 3                                      # winner, unnamed, listener
    obscure = (vault / "people" / "obscure_researcher.md").read_text()
    assert ap.NO_KNOWLEDGE in obscure and "per model knowledge" not in obscure.split("## As stated")[0]


@pytest.mark.parametrize("cutoff,verify", [("2026-06", True), ("2026-12", False)])
def test_vague_role_resolution_annotates_notes_and_logs(tmp_path, cutoff, verify):
    vault = tmp_path / "vault" / "subj"
    body = ("- The deal was criticised by the Secretary of State on Monday [0:02:00]\n"
            "- Later the Secretary of State repeated it [0:03:00]\n"
            "- Nvidia CEO Jensen Huang agreed [0:04:00]\n")
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14",
                 [("NLW", "host", "Host", "Hosts.", "[0:01:00]")], body=body)]
    llm = FakeLLM(cutoff=cutoff, roles={"the Secretary of State": "Marco Rubio"})
    res = ap.run(_scope(tmp_path, paths), llm)
    assert [r["reference"] for r in llm.calls[0][1]] == ["the Secretary of State"]
    assert llm.calls[0][1][0]["episode_date"] == "2026-10-14"
    notes = paths[0].read_text()
    if verify:
        want = ("the Secretary of State (→ [Marco Rubio](../people/marco_rubio.md)?, as of 2026-10-14, "
                "per model knowledge — verify — after model knowledge cutoff)")
    else:
        want = ("the Secretary of State (→ [Marco Rubio](../people/marco_rubio.md), as of 2026-10-14, "
                "per model knowledge)")
    assert want in notes
    assert notes.count("(→ ") == 1                     # first mention only
    assert (vault / "people" / "marco_rubio.md").exists()
    log = (vault / "people" / "_resolutions.md").read_text()
    assert "the Secretary of State | 2026-10-14 | [Marco Rubio](marco_rubio.md) | high" in log
    assert (ap.VERIFY in log) is verify
    assert res.roles[0]["verify"] is verify and res.notes_annotated == 1
    # the digest never sees the annotation; a rerun makes no call, no new annotation
    assert "per model knowledge" not in dg.load_episode(paths[0]).body
    llm2 = FakeLLM(cutoff=cutoff)
    again = ap.run(_scope(tmp_path, paths), llm2)
    assert again.calls == 0 and paths[0].read_text() == notes


def test_sources_index_ranks_listen_read_queue(tmp_path):
    vault = tmp_path / "vault" / "subj"
    p1 = _ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", [("NLW", "host", "Host", "-", "[0:01:00]")],
             sources=[("The Information", "outlet", "host", "Funding.", "-", "[0:01:00] [0:02:00]"),
                      ("Hard Fork", "podcast", "host", "Agents.", "-", "[0:03:00]")])
    p2 = _ep(vault, "ep2", "BBBBBBBBBBB", "2026-10-20", [("NLW", "host", "Host", "-", "[0:01:00]")],
             sources=[("the Information", "outlet", "host", "Revenue.", "-", "[0:01:00]")])
    res = ap.run(_scope(tmp_path, [p1, p2]), FakeLLM())
    idx = (vault / "sources" / "_index.md").read_text()
    assert "Candidate inputs for interests and listening" in idx
    queue = idx.split("## Listen / read next")[1].split("## All sources")[0]
    assert queue.index("**The Information**") < queue.index("**Hard Fork**")
    assert "(podcast; listen)" in queue and "(outlet; read)" in queue
    row = next(ln for ln in idx.split("\n") if ln.startswith("| The Information"))
    assert "| outlet | 3 | 2 | 2026-10-14 | 2026-10-20 |" in row
    assert res.sources[0] == {"source": "The Information", "times": 3, "episodes": 2}
    assert not res.link_problems


def test_role_refs_skip_named_and_keep_multiword_orgs(tmp_path):
    vault = tmp_path / "vault" / "subj"
    body = ("- Brian Moynihan is Bank of America's CEO, per model knowledge [0:02:00]\n"
            "- The late-2023 ousting of OpenAI's CEO shook the deal [0:03:00]\n"
            "- Bank of America's CEO warned about rates [0:04:00]\n"
            "- JPMorgan's CEO is Jamie Dimon [0:05:00]\n")
    p = _ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", [("NLW", "host", "Host", "-", "[0:01:00]")], body=body)
    refs = ap.find_role_refs(ap._episodes_from([p]))
    assert sorted(r.phrase for r in refs) == ["Bank of America's CEO", "OpenAI's CEO"]


def test_possessive_role_is_not_a_reference(tmp_path):
    vault = tmp_path / "vault" / "subj"
    body = "- A report under the President's executive order, by the President's Council [0:02:00]\n"
    p = _ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-16", [("NLW", "host", "Host", "-", "[0:01:00]")], body=body)
    assert ap.find_role_refs(ap._episodes_from([p])) == []
