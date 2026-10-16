## Running ledger — end your reply with it

After all sections, end your reply with a fenced code block whose info string
is `ledger`, containing ONE JSON object. The next part of this chapter receives
it as context, so it must let a reader continue mid-chapter:

```ledger
{
  "concepts": [{"term": "agentic loop", "definition": "model calls tools until a stop condition", "status": "observed"}],
  "claims": [{"claim": "rewriting the rule as Bash(npx *) blocks npx with arguments", "status": "expected", "ts": "[8:54:41]"}],
  "terms_and_corrections": ["'claw' in the transcript = Claude"],
  "open_threads": ["started building the support agent; tool definitions next"],
  "last_point": "one or two sentences: where this passage ended"
}
```

- Every concept and claim carries `"status"`: `"observed"` (the transcript
  shows it happen or the speaker states it as fact), `"expected"` (attempted
  or predicted, result not shown), or `"superseded"` (later corrected or
  contradicted — say by what). Never record an expected outcome as observed;
  when this passage shows the result, update the status.
- `claims` holds outcomes and conclusions from demos and tests (what worked,
  what was blocked), each with the `ts` of the line that shows it.
- If a previous ledger was given, CARRY IT FORWARD: keep its entries (shorten
  them if needed) and add this passage's.
- Keep the whole ledger under about 600 words. Definitions are one line.
- Valid JSON only (double quotes, no comments, no trailing commas).
