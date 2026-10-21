## Sources Referenced — what this passage cites

Add a section under the heading `## Sources Referenced`, placed immediately
after `## People`. It lists the outside sources this passage quotes, cites or
relies on — so a reader can tell where a claim came from and find the
original. It is a markdown table with exactly these columns, in this order:

| source | type | cited by | cited for | date / edition | citations |
|---|---|---|---|---|---|
| <name as given> | <type> | <who cites it here> | <what it was cited for> | <date if stated> | [H:MM:SS] [H:MM:SS] |

Who gets a row — one row per source:

- Outlets and publications (a newspaper, magazine, newsletter, blog, wire
  service), shows and podcasts, papers and reports, books, posts (a tweet/X
  post, a blog post, a forum thread), datasets, benchmarks and surveys that
  are cited for a finding, videos and talks, company announcements or filings
  cited as documents.
- Only sources this passage actually cites: quoted, read from, summarized, or
  named as the basis of a claim ("according to The Information …",
  "a new Pew survey found …", "on the Dwarkesh podcast she said …").
- NOT people. A person goes in the People table, never here — even when they
  are the one being quoted. A person's OWN outlet is a source: their podcast,
  newsletter, blog, paper or book gets a row here, and the person stays in
  People. ("Ben Thompson wrote in Stratechery" → Stratechery is a source,
  Ben Thompson is in People.)
- NOT products, models or companies that are only discussed — those are key
  terms. A company is a source only when one of its documents is cited (a
  blog post, a system card, a filing): name the document.
- NOT this source itself (the show or document you are summarizing), and
  not generic phrases with nothing identifiable ("some reports say",
  "people on Twitter") — leave those out.

Column rules:

- **source** — the name as the source gives it ("The Information",
  "Stratechery", "the METR report on task length"). Never expand or correct
  it from your own knowledge. When the transcript is machine-made and the
  name is uncertain, add `(ASR spelling)`.
- **type** — one of: outlet, newsletter, blog, podcast, show, paper, report,
  book, post, dataset, survey, video, talk, announcement, filing, other.
- **cited by** — who cites it IN THIS SOURCE, by their role here: host,
  guest, speaker, author, interviewer (`host` when the host reads it out).
  Use the role, not the name — names belong in People.
- **cited for** — one sentence: what the source was cited for (the claim,
  finding, number or quote), attributed — "reported by The Information, per
  the host: …". Do not judge or extend the claim.
- **date / edition** — only a date, issue, edition or version the passage
  states; otherwise `-`.
- **citations** — the timestamps of the lines where the source is cited, up
  to four, oldest first. The citation rules above apply.

No outside knowledge: everything in the row comes from this passage. If one
fact the passage does not give is essential to identify the source (what an
acronym stands for, which outlet a well-known show belongs to), add at most
one short clause labelled `(per model knowledge)`.

Order the rows by first citation. If the passage cites no outside source,
still write the heading, the header row and the separator row, then a line
`- None`.

Other sections keep attributing claims to their sources in prose
("The Information reports …"); do not copy the table into them.
