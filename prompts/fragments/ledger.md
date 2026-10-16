## Running ledger — end your reply with it

After all sections, end your reply with a fenced code block whose info string
is `ledger`, containing ONE JSON object. The next part of this chapter receives
it as context, so it must let a reader continue mid-chapter:

```ledger
{
  "concepts": [{"term": "agentic loop", "definition": "model calls tools until a stop condition"}],
  "terms_and_corrections": ["'claw' in the transcript = Claude"],
  "open_threads": ["started building the support agent; tool definitions next"],
  "last_point": "one or two sentences: where this passage ended"
}
```

- If a previous ledger was given, CARRY IT FORWARD: keep its entries (shorten
  them if needed) and add this passage's.
- Keep the whole ledger under about 600 words. Definitions are one line.
- Valid JSON only (double quotes, no comments, no trailing commas).
