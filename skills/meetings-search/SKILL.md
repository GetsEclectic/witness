---
name: meetings-search
description: Search recorded meetings by keyword or phrase across transcripts and summaries. Use for "what did we say about <topic>", "find the meeting where we decided X", or "show recent conversations about <person/project>".
---

# meetings-search

The witness pipeline stores each recorded meeting at `$WITNESS_MEETINGS_DIR/<slug>/` (default `~/meetings/<slug>/`) with:

- `transcript.md` — human-readable, speaker-labeled transcript
- `transcript.jsonl` — raw ASR events (one per line, with `channel` and `ts_start`)
- `summary.md` — Claude-generated TL;DR / decisions / action items
- `metadata.json` — calendar event, attendees, started/ended timestamps
- `audio.opus` — 2-channel original recording (mic + system)

Speakers are attributed by audio channel, not by voice: the mic channel is **You** (the person running witness) and the system channel is **Remote** (everyone else, undifferentiated). To tell remote speakers apart, use `metadata.json`'s attendee list plus context in the transcript.

## How to use this skill

1. **Parse what the user is asking for.** Is it a keyword (grep), a person (filter by `attendees`), or a time range ("last week")?

2. **Search, broadly then narrowly.**
   - `rg -i "<query>" "$WITNESS_MEETINGS_DIR"/*/transcript.md "$WITNESS_MEETINGS_DIR"/*/summary.md -l` to find candidate folders.
   - For person-scoped queries: `rg -l "<name>" "$WITNESS_MEETINGS_DIR"/*/metadata.json` (attendees), then read the transcript for what they said.
   - For date ranges: use folder name prefixes (e.g. `"$WITNESS_MEETINGS_DIR"/2026-04-*`).

3. **Read the matches.** Open `summary.md` first (dense, pre-digested). If the user wants more depth or the summary doesn't contain the answer, fall back to `transcript.md`.

4. **Report with anchors.** Include folder slug + speaker + timestamp for each excerpt: `2026-04-16T1200-team-1on1 · Remote [12:34]`. Timestamps from `transcript.jsonl` are seconds-from-start; folder slug + `audio.opus` lets the user jump straight to the moment.

5. **Don't fabricate.** If the transcripts don't answer the question, say so. Never summarize a meeting you didn't actually read.

## Output shape

- Lead with a direct answer (1–2 sentences) if the transcripts support one.
- Follow with up to 3 quoted excerpts, each with `slug · You|Remote [MM:SS]` anchor.
- Close with the list of meeting slugs searched, so the user can audit.

## Edge cases

- **Who is "Remote"**: every non-user voice shares one label. Don't attribute a quote to a named attendee unless the transcript itself makes it unambiguous (they're addressed by name, or they're the only other attendee). Say "someone on the call" otherwise.
- **Older transcripts**: meetings recorded before 2026-08 may carry per-utterance `speaker_N` labels left over from the removed diarization step. Ignore them — they attributed badly, which is why they're gone. The `channel` field is the reliable signal.
- **No audio / no summary**: some meetings only have `transcript.md`. Work with what's there.
- **Empty meetings dir**: the pipeline may not have recorded anything yet. Say so plainly rather than inventing results.
