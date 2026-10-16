# Long-form notes: outline a recording with no chapters

You are given a COMPRESSED transcript of one long recording (a meetup, panel,
lecture series or course). It is not the full text: there is one line about
every 30 seconds (its first words only), plus every line that looks like a
talk transition (thanks, applause, introductions, "my name is", long pauses).
Each line starts with its timestamp `[H:MM:SS]`.

Split the recording into the sections a learner would want as chapters:
separate talks/speakers first, then major topic shifts within a long talk.

Rules:

- 3–15 sections in time order, covering the whole recording from the start.
- Each section at least ~5 minutes, unless it is clearly a separate segment
  (welcome/intro, sponsor slot, Q&A, break, closing).
- `start` is the timestamp of the line where the section begins, copied from
  the transcript in `H:MM:SS` form. A new talk begins where the host
  introduces the speaker or the speaker introduces themself.
- `title` is a short descriptive title (at most ~10 words) of what the section
  is about, taken from what is said — never "Section 3".
- `speaker` is the speaker's name as spoken in the transcript, or null when no
  name is given. Do not guess names or employers.

Reply with ONLY a JSON array and nothing else, for example:

[{"start": "0:00:00", "title": "Welcome and sponsors", "speaker": null},
 {"start": "0:06:10", "title": "Getting AI agents to production", "speaker": "Jane Doe"}]
