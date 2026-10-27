---
name: meetings-action-items
description: Surface open action items from recorded meetings. Use when the user asks "what did I commit to", "what's on my plate from this week's meetings", or "what action items are still open".
---

# meetings-action-items

Three sources, most structured first:

1. **The tracker** — `~/.local/state/witness-actions/items.jsonl` (override:
   `$WITNESS_ACTIONS_DIR`), one JSON line per item the user owns, across
   meetings since 2026-10-27. Each has `id`, `slug`, `text`, `kind`, `due`,
   `people`, `status` (`open | drafted | needs_input | done | dropped`),
   `artifacts` (links to Slack drafts, paths to documents the follow-up worker
   wrote) and `note` (the worker's open question). Read it with
   `witness actions list` (`--all` includes done and dropped).
2. **`$WITNESS_MEETINGS_DIR/<slug>/action_items.json`** — every item from one
   meeting, everyone's, with `owner` and `is_mine`. Use it for "what did
   <Person> commit to".
3. **`summary.md`'s `## Action items` section**, for meetings older than the
   tracker. Lines are `- <owner>: <what> (due <when>) [MM:SS]`; summaries
   written before September 2026 use `- [ ] <owner>: <what>`, and sometimes name
   the owner `You` (the user) or `Remote` (audio channel labels, not people).

## How to use this skill

1. **Scope.** Default to the last 14 days of meetings unless the user says otherwise.
2. **Read the tracker first**, then fill in older meetings from summary.md:
   ```
   rg -A 20 "^## Action items" "$WITNESS_MEETINGS_DIR"/*/summary.md
   ```
3. **Filter by ownership.** Default to the user's own items. For someone else,
   use `action_items.json` and filter on `owner`.
4. **Show what is already drafted.** An item with status `drafted` has a draft
   waiting for review — list its artifact links so the user can send it.
   `needs_input` items carry the worker's question in `note`; surface it.
   The meeting's `followup/` directory holds the brief and any documents.
5. **Present** a checklist grouped **Overdue / today**, **This week**,
   **Later / undated**: `- [ ] <action> — <status> — from <slug> (due <when>)`.

## Changing state

When the user says an item is done or no longer needed, run
`witness actions done <id>` or `witness actions drop <id>` (an id prefix is
enough). Items that exist only in summary.md have no state to change; say so.

## What not to do

- **Don't invent action items.** If the sources say nothing, say nothing.
- **Don't dedupe aggressively.** Two meetings that mention "confirm Q2 headcount"
  are probably related but not always identical — flag, don't merge.
- **Don't mark anything done on your own judgement**, only on the user's word.

## Edge case

If a recent meeting has no `summary.md` or no `action_items.json`, the pipeline
may still be running or a step failed. Check `witness.log` in that folder and,
for the follow-up worker, `followup/worker.log`. Mention the gap; don't skip it.
