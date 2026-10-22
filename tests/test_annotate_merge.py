"""WP-P5: duplicate people merged after a lookup (deterministic, 0 model
calls), and the tighter "AI-related bio" rule. Mocked LLM / web; assertions
on the files a reader gets (notes, index, voices table, redirects, links)."""

import datetime as dt
import json

from test_annotate_people import FakeLLM, _ep, _scope, _snapshot
from test_annotate_web import AS_OF, FakeWeb, voice_reply
from video2mdnotes.core import annotate_people as ap


class BioLLM(FakeLLM):
    """FakeLLM with chosen descriptions / canonical spellings per name."""

    def __init__(self, bios=None, canonical=None, **kw):
        super().__init__(**kw)
        self.bios, self.canonical = bios or {}, canonical or {}

    def __call__(self, system, user):
        text, model = super().__call__(system, user)
        data = json.loads(text)
        for p in data["people"]:
            name = next(x["names"][0] for x in self.calls[-1][0] if x["id"] == p["id"])
            if name in self.bios:
                p["description"] = self.bios[name]
            if name in self.canonical:
                p["canonical_name"] = self.canonical[name]
        return json.dumps(data), model


THEO_BIO = ("Theo Browne (\"t3.gg\") is a developer and YouTuber who covers web development and AI tools. "
            "He created the T3 Stack and previously worked at Twitch.")
POST = "https://x.com/theo/status/1"


def _kyo_theo(tmp_path, body=""):
    vault = tmp_path / "vault" / "subj"
    rows = [("Theo", "quoted", "developer and YouTuber", "Says Argon fails his bench.", "[0:17:00]"),
            ("Kyo (ASR spelling)", "quoted", "not stated",
             "Calls it \"a huge jump in quality\".", "[0:22:48]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows, body=body)]
    llm = BioLLM(bios={"Theo": THEO_BIO}, unknown={"Kyo"})
    rep = voice_reply("confirmed", "theo", [(POST, "post", False, ["quote", "topic"]),
                                            ("https://x.com/theo", "profile", False, ["bio"])],
                      quote_url=POST, affiliation="developer and creator of t3.gg", atype="founder",
                      description="Developer behind t3.gg who tests frontier models.")
    rep["display_name"] = "Theo - t3.gg"
    web = FakeWeb(voices={"Kyo": rep})
    return vault, paths, llm, web


def test_lookup_reveals_duplicate_kyo_theo_merged(tmp_path):
    body = "- Kyo's note is at [Kyo](../people/kyo.md) [0:22:48].\n"
    vault, paths, llm, web = _kyo_theo(tmp_path, body=body)
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=web, as_of=AS_OF)
    assert res.calls == 1 and len(web.calls) == 1                 # the description + the voice lookup
    assert [m["from"] for m in res.merges] == ["kyo"] and res.merges[0]["into"] == "theo"
    assert "corroborated by \"t3.gg\"" in res.merges[0]["basis"]
    assert res.people == 1 and not res.link_problems
    for root in (tmp_path / "collection", vault):
        theo = (root / "people" / "theo.md").read_text()
        # the survivor is the identity's own name; aliases + appearances unioned
        assert 'aliases: ["Kyo"]' in theo and "appearances: 1" in theo
        assert "Says Argon fails his bench." in theo and "a huge jump in quality" in theo
        # the richer description (a bio) kept, the other one kept with its provenance
        assert "## Description — per model knowledge (cutoff uncertain)\n\n" + THEO_BIO in theo
        assert ("- Also, per web lookup 2026-10-21 (confirmed identity), from \"Kyo\": "
                "Developer behind t3.gg who tests frontier models.") in theo
        assert '"Kyo" (was [kyo](kyo.md)) — handle / display name "theo" matches the name' in theo
        # the identity found for Kyo now belongs to Theo
        assert "_The identity below was found for the merged record \"Kyo\"._" in theo
        assert "- Handle: [@theo](https://x.com/theo)" in theo
        # the dropped note is a redirect
        kyo = (root / "people" / "kyo.md").read_text()
        assert 'merged_into: "theo"' in kyo and "Merged into [Theo](theo.md)" in kyo
        idx = (root / "people" / "_index.md").read_text()
        assert "people: 1" in idx and "| [Kyo]" not in idx
        assert '- "Kyo" ([kyo](kyo.md)) → [Theo](theo.md) — handle' in idx
        # one AI-voices row, both quotes counted, the confirmed handle shown
        s = (root / "sources" / "_index.md").read_text()
        sec = s.split("## AI voices", 1)[1].split("\n## ", 1)[0]
        rows = [ln for ln in sec.splitlines() if ln.startswith("| ") and not ln.startswith("| #")]
        assert len(rows) == 1
        assert rows[0].startswith("| 1 | [Theo](../people/theo.md) · [@theo](https://x.com/theo) | Theo")
        assert "| confirmed | 2 | 1 |" in rows[0]
    # links to the dropped note in the episode notes point to the survivor
    notes = paths[0].read_text()
    assert "[Kyo](../people/theo.md)" in notes and "people/kyo.md" not in notes
    assert res.links_redirected == 1
    state = json.loads((tmp_path / "collection" / "people" / ap.STATE_FILE).read_text())
    assert state["merges"]["kyo"]["into"] == "theo" and state["merges"]["kyo"]["merged_at"] == "2026-10-21"
    assert "voice" in state["people"]["kyo"] and "voice" not in state["people"]["theo"]  # caches intact
    # rerun: 0 model calls, 0 web calls, byte-identical
    snap = _snapshot(tmp_path)
    llm2, web2 = BioLLM(), FakeWeb()
    res2 = ap.run(_scope(tmp_path, paths), llm2, web_complete=web2, as_of=AS_OF)
    assert res2.calls == 0 and web2.calls == [] and llm2.calls == []
    assert [m["from"] for m in res2.merges] == ["kyo"]
    assert _snapshot(tmp_path) == snap


def test_new_spelling_of_known_identity_is_not_described_again(tmp_path):
    """ASR variant: the model knew "Jon Smyth" as John Smith; a later episode
    spells it right — merged BEFORE the model call (0 calls), the correct
    spelling becomes the note, the old note a redirect."""
    vault = tmp_path / "vault" / "subj"
    p1 = _ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14",
             [("Jon Smyth", "quoted", "CEO of Acme Robotics", "Said robots ship.", "[0:01:00]")])
    llm = BioLLM(bios={"Jon Smyth": "John Smith is the CEO of Acme Robotics."},
                 canonical={"Jon Smyth": "John Smith"})
    ap.run(_scope(tmp_path, [p1]), llm, as_of=AS_OF, web=False)
    assert (vault / "people" / "jon_smyth.md").exists()
    p2 = _ep(vault, "ep2", "BBBBBBBBBBB", "2026-10-16",
             [("John Smith", "quoted", "Acme Robotics CEO", "Said more robots ship.", "[0:02:00]")])
    llm2 = BioLLM()
    res = ap.run(_scope(tmp_path, [p1, p2]), llm2, as_of=AS_OF, web=False)
    assert res.calls == 0 and llm2.calls == []
    assert [(m["from"], m["into"]) for m in res.merges] == [("jon_smyth", "john_smith")]
    assert 'identity name "john smith" matches' in res.merges[0]["basis"]
    note = (vault / "people" / "john_smith.md").read_text()
    assert 'aliases: ["Jon Smyth"]' in note and "appearances: 2" in note
    assert "John Smith is the CEO of Acme Robotics." in note
    assert "merged_into: \"john_smith\"" in (vault / "people" / "jon_smyth.md").read_text()
    assert not res.link_problems


def test_bare_first_name_without_corroboration_does_not_merge(tmp_path):
    """A confirmed handle equal to someone else's one-word name is not enough:
    the two records must share a distinctive token."""
    vault = tmp_path / "vault" / "subj"
    rows = [("Sam", "quoted", "banker", "Said rates matter.", "[0:01:00]"),
            ("Sem", "quoted", "not stated", "Calls it \"a big agent jump\".", "[0:02:00]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows)]
    llm = BioLLM(bios={"Sam": "Sam is a banker at Northbank in Ohio."}, unknown={"Sem"})
    rep = voice_reply("confirmed", "sam", [("https://x.com/sam/status/1", "post", False, ["quote"]),
                                           ("https://x.com/sam", "profile", False, ["bio"])],
                      quote_url="https://x.com/sam/status/1", affiliation="builds Zapcat agents")
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=FakeWeb(voices={"Sem": rep}), as_of=AS_OF)
    assert res.merges == [] and res.people == 2


def test_private_and_probable_never_merge(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [("Theo", "quoted", "developer", "Says things.", "[0:17:00]"),
            ("Kyo", "quoted", "not stated", "Calls it \"a huge jump in quality\".", "[0:22:48]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-14", rows)]
    llm = BioLLM(bios={"Theo": THEO_BIO}, unknown={"Kyo"})
    rep = voice_reply("probable", "theo", [("https://x.com/theo", "profile", False, ["bio", "topic"])],
                      affiliation="creator of t3.gg")
    res = ap.run(_scope(tmp_path, paths), llm, web_complete=FakeWeb(voices={"Kyo": rep}), as_of=AS_OF)
    assert res.merges == []                       # a probable identity is not enough to merge
    sp = tmp_path / "collection" / "people" / ap.STATE_FILE
    state = json.loads(sp.read_text())
    state["people"]["kyo"]["voice"]["tier"] = "confirmed"
    state["people"]["kyo"]["voice"]["display_name"] = "Theo - t3.gg"
    state["people"]["theo"]["private"] = True
    sp.write_text(json.dumps(state))
    res = ap.run(_scope(tmp_path, paths), BioLLM(), web_complete=FakeWeb(), as_of=AS_OF)
    assert res.merges == []                       # never into / from a private record


def test_redirect_links_only_touches_people_links(tmp_path):
    f = tmp_path / "n.md"
    f.write_text("[K](../people/kyo.md) [K2](people/kyo.md) [other](../kyo.md) [x](../people/kyo_two.md)\n")
    assert ap.redirect_links([f], {"kyo": "theo"}) == 1
    assert f.read_text() == ("[K](../people/theo.md) [K2](people/theo.md) [other](../kyo.md) "
                             "[x](../people/kyo_two.md)\n")


def test_ai_related_is_about_the_person_not_an_event():
    # a judge in an AI case: the AI term is inside the event clause
    judge = ("Alex G. Tse is a US Magistrate Judge in the Northern District of California. On October 19, "
             "2026, he denied bail to Abdulrahman Alfozan, the former OpenAI engineer accused of bugging a room.")
    assert not ap.ai_related(judge)
    # the identity clause comes before the event clause: still AI-related
    assert ap.ai_related("Abdulrahman Alfozan is a former OpenAI engineer who was arrested in October 2026.")
    assert ap.ai_related("Colin Adams is the CEO of Character.AI.")
    assert ap.ai_related("Jane Doe is a journalist who writes about AI policy.")
    assert not ap.ai_related("Bo Banker is CEO of a bank. He testified at the OpenAI antitrust trial.")
    # word-boundary fixes: computer science, FBI agents, Neuralink-free bios
    assert not ap.ai_related("Jane is a professor of computer science at a state college.")
    assert not ap.ai_related("Kash Patel leads the FBI and its special agents.")
    assert ap.ai_related("Dax Raad works on OpenCode, an open-source AI coding agent.")


def test_judge_in_an_ai_case_is_not_an_ai_voice(tmp_path):
    vault = tmp_path / "vault" / "subj"
    rows = [("Alex Tse", "quoted", "judge", "Denied bail.", "[0:06:52]"),
            ("Ada Lab", "quoted", "researcher", "Said agents work.", "[0:02:00]")]
    paths = [_ep(vault, "ep1", "AAAAAAAAAAA", "2026-10-20", rows)]
    llm = BioLLM(bios={"Alex Tse": "Alex Tse is a US Magistrate Judge. In October 2026 he denied bail to "
                                   "a former OpenAI engineer.",
                       "Ada Lab": "Ada Lab is a research scientist at Anthropic."})
    ap.run(_scope(tmp_path, paths), llm, as_of=AS_OF, web=False)
    s = (vault / "sources" / "_index.md").read_text()
    sec = s.split("## AI voices", 1)[1].split("\n## ", 1)[0]
    assert "[Ada Lab]" in sec and "[Alex Tse]" not in sec
    assert "1 identified quoted people whose description is not AI-related" in sec
