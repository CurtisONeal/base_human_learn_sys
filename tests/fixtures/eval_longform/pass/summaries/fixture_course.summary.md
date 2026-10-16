---
title: "Fixture course"
published: 2026-07-20
as_of: 2026-10-15
source: https://www.youtube.com/watch?v=fixture
---

# Fixture course

## 1. Introduction [0:00:00]

- The course covers every exam domain [0:00:10].
- Claude Code is the CLI agent you run in a terminal; written in lowercase,
  claude code is still the right name [0:01:20–0:01:40].
- Labs run on any cloud provider and assume basic cloud computing [01:50].

## 2. SDK Environment Setup [0:03:20]

- Install the SDK with pip [0:03:30].
- Keep the API key in an environment variable [0:04:00 - 0:04:30].
- Never commit the key to git [04:30].
- Rate limits are 50 requests per minute at time of recording [0:05:00].

## 3. Agentic Loop Foundations

- The loop ends when stop_reason is `end_turn` [0:07:00].
- Tool results go back to the model as user messages [0:08:15].
- A demo shows the loop running ([0:09:00](https://www.youtube.com/watch?v=fixture&t=540)).

This paragraph cites [0:06:00] but is not a bullet, so P8 does not sample it.

## Time-Sensitive Claims

| claim | timestamp | as-of | type | verify-by |
|---|---|---|---|---|
| Rate limit is 50 requests per minute | 0:05:00 | 2026-07-20 | quota | 2027-01-01 |
| The exam costs $99 | [0:00:30] | 2026-07 | price | 2026-12-31 |
