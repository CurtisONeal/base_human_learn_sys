MERGING PARTIAL DIGESTS

This time the input is not episode notes but PARTIAL DIGESTS: JSON objects
in the schema above, each covering a consecutive part of the span. Their
citations already use this span's episode labels (`E<n> H:MM:SS`). Merge them
into ONE digest of the whole span, with the same schema:

- Merge stories that are the same story across parts into one top story,
  and keep the union of their citations and segments. Copy citations and
  segment times exactly from the partials. Never write a new timestamp.
- A story that appears in more than one part is a thread: give its
  developments in date order and its latest state.
- `people`: the people who matter most across the whole span.
- `superseded`: use the TIME-SENSITIVE CLAIMS ids listed below. Pairs found in
  a partial digest are not carried over automatically, so list every pair
  again.
- Everything you write must still be supported by a citation present in the
  partials.
