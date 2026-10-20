"""WP-D1: the document path — one call for short documents, packed passages for
long ones, one Overview / one Key terms, and balanced code fences.

Assertions are on artifacts: the prompts the fake model receives, the call
count, and the notes text that would be written.
"""

import re
from pathlib import Path


from video2mdnotes.core import docnotes, fences, summarizer
from video2mdnotes.core.chunking import DEFAULT_MAX_WORDS, pack_sections
from video2mdnotes.core.documents import plaintext
from video2mdnotes.core.sources import Section, SourceText

FENCE = "```"


def _words(n, seed="word"):
    return " ".join(f"{seed}{i % 97}" for i in range(n))


def _nine_k_note(tmp_path: Path) -> Path:
    """~9,000 words shaped like the T8 Entra note: an H2 before the H1, many
    H2/H3 sections, and bash blocks whose `# comment` lines look like headings."""
    parts = ["## Agent Summary", "", _words(150, "sum"), "",
             "# Entra ID for AWS engineers", "", _words(200, "intro"), ""]
    for i in range(1, 15):
        parts += [f"## Topic {i}", "", _words(300, f"t{i}a"), "",
                  f"### Detail {i}", "", _words(280, f"t{i}b"), "",
                  f"{FENCE}bash",
                  f"# Creates thing {i}; --append keeps existing credentials",
                  f"az ad app create --display-name app{i}",
                  "# 1b. second step",
                  f"az ad sp create --id app{i}",
                  FENCE, ""]
    path = tmp_path / "entra-like.md"
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


class FakeModel:
    """Records every (system, user) pair; replies by prompt kind."""

    def __init__(self, whole=None, passage=None, reduce=None, overview=None):
        self.calls = []
        self.whole, self.passage, self.reduce, self.overview = whole, passage, reduce, overview

    def kinds(self):
        return [k for k, _, _ in self.calls]

    def __call__(self, system, user):
        if system == summarizer._WHOLE_SYSTEM:
            kind, fn = "whole", self.whole
        elif system == summarizer._CHUNK_SYSTEM:
            kind, fn = "passage", self.passage
        elif system == summarizer._REDUCE_SYSTEM:
            kind, fn = "reduce", self.reduce
        elif system == summarizer._OVERVIEW_SYSTEM:
            kind, fn = "overview", self.overview
        else:
            raise AssertionError(f"unexpected system prompt: {system[:60]!r}")
        self.calls.append((kind, system, user))
        return fn(user, len(self.calls)), "claude-cli/test"


def _cited_locators(user):
    return re.findall(r"^\[([^\]\n]+)\]$", user, re.M)


# --------------------------------------------------------------------------- #
# Short document: ONE call
# --------------------------------------------------------------------------- #

def test_nine_thousand_word_note_is_one_call_with_every_locator(tmp_path, monkeypatch):
    source = plaintext.extract(_nine_k_note(tmp_path))
    assert 8_500 <= source.word_count <= DEFAULT_MAX_WORDS

    def whole(user, n):
        locs = _cited_locators(user)
        body = "\n".join(f"- point from that section [{loc}]" for loc in locs)
        return ("# Entra ID for AWS engineers\n\n## Overview\n\n" + _words(80) + "\n\n"
                "## Topics\n\n" + body + "\n\n" + _words(300) + "\n\n"
                "## Key terms\n\n- **App role**: an entitlement [§Topic 1]\n")

    fake = FakeModel(whole=whole)
    monkeypatch.setattr(summarizer, "complete_with_failover", fake)
    result = summarizer.summarize_document(source)

    assert fake.kinds() == ["whole"]                        # exactly one model call
    user = fake.calls[0][2]
    for sec in source.sections:                             # every locator reached the model
        assert f"[{sec.locator}]" in user, sec.locator
    assert fences.fence_report(user).balanced               # no code block cut in half
    notes = result.summary_text
    assert notes.count("## Overview") == 1
    assert notes.count("## Key terms") == 1
    assert "passages: 1 " in notes and "model_calls: 1" in notes


def test_code_comments_are_not_headings(tmp_path):
    """Root cause of T8's 22 passages: `# comment` inside ```bash was a heading."""
    source = plaintext.extract(_nine_k_note(tmp_path))
    titles = {t for s in source.sections for t in s.path}
    assert not any("--append" in t or "second step" in t for t in titles)
    assert all(fences.fence_report(s.text).balanced for s in source.sections)
    assert all("az ad sp create" in s.text for s in source.sections if "az ad app create" in s.text)
    assert len(pack_sections(source.sections)) == 1


# --------------------------------------------------------------------------- #
# Long document: packed whole chapters, one Overview, one merged Key terms
# --------------------------------------------------------------------------- #

def _book(chapter_words):
    sections = []
    for n, words in enumerate(chapter_words, 1):
        half = words // 2
        for k, w in enumerate((half, words - half), 1):
            sections.append(Section(path=[f"Chapter {n}", f"Section {n}.{k}"],
                                    locator=f"ch. {n} §Section {n}.{k}",
                                    text=_words(w, f"c{n}s{k}w"), word_count=w))
    return SourceText(source_id="book", title="Long Book", kind="document",
                      origin="/tmp/long.epub", sections=sections,
                      provenance="epub (toc)")


def test_long_book_packs_whole_chapters_and_splits_only_an_oversized_one():
    source = _book([5_000, 5_000, 5_000, 5_000, 15_000, 3_000])
    chunks = pack_sections(source.sections)
    tops = [sorted({s.path[0] for s in c.sections}) for c in chunks]
    # ch1+ch2 (10k), ch3+ch4 (10k), ch5 alone is over budget → its two sections,
    # ch6 starts fresh after the split chapter.
    assert tops == [["Chapter 1", "Chapter 2"], ["Chapter 3", "Chapter 4"],
                    ["Chapter 5"], ["Chapter 5"], ["Chapter 6"]]
    assert all(c.word_count <= DEFAULT_MAX_WORDS for c in chunks)
    # Seams fall between chapters: no chapter appears in two chunks unless split.
    for name in ("Chapter 1", "Chapter 2", "Chapter 3", "Chapter 4", "Chapter 6"):
        assert sum(name in t for t in tops) == 1


def test_long_book_has_one_overview_and_one_merged_key_terms(monkeypatch):
    source = _book([5_000] * 16)              # 80k words → 8 packed passages → 2 batches

    def passage(user, n):
        loc = _cited_locators(user)[0]
        return (f"## Overview\n\nPassage overview that must not survive.\n\n"
                f"## Topic from {loc}\n\n" + _words(200, f"p{n}w") + f" [{loc}]\n\n"
                f"#### Key terms\n\n- **Service principal**: the app's identity [{loc}]\n"
                f"- **Term {n}**: defined here [{loc}]\n")

    def reduce(user, n):
        heads = re.findall(r"^## (Topic from .+)$", user, re.M)
        # A disobedient reduce: its own title, Overview and Key terms per batch.
        return ("# Long Book\n\n## Overview\n\nBatch overview.\n\n"
                + "\n\n".join(f"## {h}\n\n" + _words(200, f"r{n}w") for h in heads)
                + "\n\n## Key terms\n\n- **Service principal**: duplicate [ch. 99 §X]\n")

    def overview(user, n):
        assert "Topic from" in user
        return "This book explains identity end to end. " + _words(60, "ov")

    fake = FakeModel(passage=passage, reduce=reduce, overview=overview)
    monkeypatch.setattr(summarizer, "complete_with_failover", fake)
    result = summarizer.summarize_document(source)
    notes = result.summary_text

    kinds = fake.kinds()
    assert kinds.count("passage") == 8 and kinds.count("reduce") == 2
    assert kinds.count("overview") == 1 and "whole" not in kinds
    # Passages' key terms are split off before the reduce sees them.
    for kind, _, user in fake.calls:
        if kind == "reduce":
            assert "#### Key terms" not in user and "Topic from" in user
    assert notes.count("## Overview") == 1
    assert "Passage overview that must not survive" not in notes
    assert "Batch overview" not in notes
    assert notes.count("## Key terms") == 1
    assert len(re.findall(r"(?m)^# ", notes)) == 1          # one title
    terms = notes.split("## Key terms", 1)[1].split("\n---\n")[0]
    sp = [line for line in terms.splitlines() if "Service principal" in line]
    assert len(sp) == 1                                      # deduped across 8 passages + reduce
    assert "[ch. 1 §Section 1.1]" in sp[0] and "[ch. 99 §X]" in sp[0]   # locators unioned
    assert sum(line.startswith("- **Term ") for line in terms.splitlines()) == 8
    assert "This book explains identity" in notes


def test_reduce_collapse_guard_still_keeps_passage_notes(monkeypatch):
    source = _book([5_000] * 4)

    def passage(user, n):
        return "## Real topic\n\n" + "real passage notes " * 200 + "\n\n#### Key terms\n\n- **X**: y\n"

    fake = FakeModel(passage=passage,
                     reduce=lambda u, n: "Great work! Do you want me to merge these?",
                     overview=lambda u, n: "unused")
    monkeypatch.setattr(summarizer, "complete_with_failover", fake)
    notes = summarizer.summarize_document(source).summary_text
    assert "Do you want me to" not in notes
    assert "real passage notes" in notes and "passage notes below are complete" in notes
    assert notes.count("## Key terms") == 1
    assert "overview" not in fake.kinds()


def test_whole_document_prompt_keeps_the_locator_and_code_rules():
    flat = " ".join(summarizer._WHOLE_SYSTEM.split()).lower()
    assert "not the passage-span locator at the top" in flat
    assert "copy every fenced block" in flat
    assert "## overview" in flat and "## key terms" in flat
    assert "each topic appears once" in flat
    assert "one passage of a longer book" not in flat


# --------------------------------------------------------------------------- #
# docnotes.consolidate
# --------------------------------------------------------------------------- #

def test_consolidate_ignores_headings_inside_code():
    text = ("# T\n\n## Overview\n\nok\n\n## A\n\n```markdown\n## Overview\n## Key terms\n```\n\n"
            "## Key terms\n\n- **a**: b\n")
    out = docnotes.consolidate(text).text
    assert out.count("## Overview") == 2          # one real + one inside the fence
    assert "```markdown\n## Overview\n## Key terms\n```" in out
    assert out.rstrip().endswith("- **a**: b")


def test_topic_named_overview_of_something_is_not_dropped():
    text = "# T\n\n## Overview\n\nx\n\n## Overview of Azure RBAC\n\nkeep me\n"
    assert "keep me" in docnotes.consolidate(text).text


def test_key_terms_table_rows_are_merged_as_entries():
    text = ("## Key terms\n\n| Term | Meaning |\n|---|---|\n| SPN | service principal |\n\n"
            "## More\n\nbody\n\n### Key terms\n\n- **SPN**: dup [§B]\n- **MI**: managed identity\n")
    out = docnotes.consolidate(text).text
    terms = out.split("## Key terms", 1)[1]
    assert out.count("Key terms") == 1
    assert [line for line in terms.splitlines() if line.startswith("- ")] == [
        "- **SPN**: service principal [§B]", "- **MI**: managed identity"]
    assert "body" in out


# --------------------------------------------------------------------------- #
# Code-fence repair
# --------------------------------------------------------------------------- #

def _balanced(text):
    rep = fences.fence_report(text)
    return rep.balanced and rep.fence_lines % 2 == 0


def test_fence_unclosed_at_end_of_file():
    text = "# T\n\n```python\nprint(1)\n"
    out, log = fences.repair_fences(text)
    assert _balanced(out) and len(log) == 1
    assert out.rstrip().endswith("print(1)\n```")


def test_fence_closed_before_next_heading_not_at_eof():
    text = "## A\n\n```bash\naz login\n\n## B\n\nprose under B\n"
    out, _ = fences.repair_fences(text)
    assert _balanced(out)
    assert "az login\n```\n\n## B\n\nprose under B" in out


def test_bash_comment_is_not_a_boundary_when_a_real_heading_follows():
    text = "```bash\n# comment\naz x\n\n### Next\n\ntext\n"
    out, _ = fences.repair_fences(text)
    assert "# comment\naz x\n```\n\n### Next" in out


def test_fence_opened_inside_a_list_item_is_closed_with_its_indent():
    text = ("1. Create the app:\n\n   ```bash\n   az ad app create --display-name x\n\n"
            "2. Next step\n\n## Heading\n\nmore\n")
    out, log = fences.repair_fences(text)
    assert _balanced(out) and log
    assert "   az ad app create --display-name x\n\n2. Next step\n   ```\n\n## Heading" in out


def test_reopened_fence_exposes_the_missing_closer():
    text = "## A\n\n```python\nx = 1\n\n## B\n\n```bash\nls\n```\n"
    out, _ = fences.repair_fences(text)
    assert _balanced(out)
    assert "x = 1\n```\n\n## B\n\n```bash\nls\n```" in out


def test_stray_bare_fence_is_removed():
    text = "## A\n\nprose\n```\n\n## B\n\nmore\n"
    out, log = fences.repair_fences(text)
    assert _balanced(out) and "removed stray fence" in log[0]
    assert "```" not in out


def test_balanced_text_inline_triple_backticks_and_tildes_are_untouched():
    text = ("Use a ``` fence for code.\n\n~~~\n```inside tilde```\n~~~\n\n"
            "````markdown\n```python\nx\n```\n````\n\n```bash\n# not a heading\n```\n")
    out, log = fences.repair_fences(text)
    assert out == text and log == []
    assert fences.fence_report(text).fence_lines == 6


def test_repair_is_idempotent():
    text = "```js\nx\n## H\n```py\ny\n"
    once, _ = fences.repair_fences(text)
    twice, log = fences.repair_fences(once)
    assert once == twice and log == [] and _balanced(once)


def test_document_notes_are_fence_repaired(tmp_path, monkeypatch):
    source = SourceText(
        source_id="n", title="N", kind="document", origin="/tmp/n.md",
        sections=[Section(path=["A"], locator="§A", text=_words(500), word_count=500)])
    fake = FakeModel(whole=lambda u, n: "# N\n\n## Overview\n\n" + _words(400)
                     + "\n\n## A\n\n```python\nprint('x')\n\n## Key terms\n\n- **x**: y\n")
    monkeypatch.setattr(summarizer, "complete_with_failover", fake)
    notes = summarizer.summarize_document(source).summary_text
    assert _balanced(notes)
    assert "print('x')\n```" in notes
    assert "code_fence_repairs: 1" in notes


def test_media_notes_writer_repairs_fences(tmp_path):
    from video2mdnotes.main import _write_notes
    dest = tmp_path / "talk.summary.md"
    _write_notes(dest, "## Chapter\n\n```\ncode\n\n## Next\n")
    assert _balanced(dest.read_text())


# --------------------------------------------------------------------------- #
# Packager: markdown sources, footer source, fence repair before notes.md
# --------------------------------------------------------------------------- #

def test_package_markdown_note_from_footer_links_sections_and_balances_fences(tmp_path):
    from video2mdnotes.core import package as pk

    note = tmp_path / "vaultsrc" / "entra.md"
    note.parent.mkdir()
    note.write_text("# Entra\n\nintro\n\n## Client secret\n\nsecret text\n\n"
                    "```bash\n# not a heading\naz ad app credential reset\n```\n\n"
                    "## Managed identity\n\nmi text\n", encoding="utf-8")
    notes = tmp_path / "run" / "entra.summary.md"
    notes.parent.mkdir()
    notes.write_text("# Entra notes\n\n## Overview\n\nAbout [§Entra].\n\n"
                     "## Secrets\n\n- Rotate secrets [§Client secret].\n\n```bash\naz login\n\n"
                     "## Identity\n\n- Prefer MI [§Managed identity].\n- Bad [§not a heading].\n"
                     f"\n---\nsource: {note}\nprovenance: markdown (headings)\n",
                     encoding="utf-8")
    res = pk.package(notes, "azure_identity", vault=tmp_path / "vault")
    assert res.report["source_from"] == "provenance footer"
    assert res.report["kind"] == "markdown"
    out = (res.package_dir / "notes.md").read_text()
    assert _balanced(out) and res.report["fence_repairs"]
    # The citation after the repaired fence is linked (the rewriter skips fences).
    assert re.search(r"\[§Managed identity\]\(text/[^)]+#Managed%20identity\)", out)
    assert re.search(r"\[§Client secret\]\(text/[^)]+#Client%20secret\)", out)
    assert res.report["citations"]["linked"] == 3
    assert [u["citation"] for u in res.report["citations"]["unresolved"]] == ["§not a heading"]
    assert pk.check_links(res.package_dir) == []
