---
name: meetings-on-topic
description: Collect everything recorded meetings say about a specific person, project, or topic across time. Use for "bring me up to speed on <project>", "what have I discussed with <person>", or "catch me up on <topic>".
---

# meetings-on-topic

The long-form counterpart to `meetings-search`. Where search returns excerpts, this skill produces a narrative digest of everything across the user's meetings on a topic.

Meetings live at `$WITNESS_MEETINGS_DIR/<slug>/` (default `~/meetings/<slug>/`).

## How to use this skill

1. **Clarify the target.** Is it a person, a project (e.g. `rate-limiter migration`), or a broader theme (`hiring`, `Q2 planning`)? The grep strategy differs:

   - **Person**: search `$WITNESS_MEETINGS_DIR/*/speakers.json` and `$WITNESS_MEETINGS_DIR/*/metadata.json` for the name / email. Include meetings they attended even if unnamed in transcripts.
   - **Project / topic**: `rg -i -l "<topic>" "$WITNESS_MEETINGS_DIR"/*/summary.md "$WITNESS_MEETINGS_DIR"/*/transcript.md` sorted by folder date.

2. **Read in chronological order.** Folder slugs start with ISO timestamps — sorting ascending gives you the arc of a topic over time.

3. **Extract the thread.** For each relevant meeting, pull:
   - the `## Decisions` and `## Action items` lines referencing the topic
   - any quoted exchange where the topic was discussed (from `transcript.md`)

4. **Write the digest.** Structure:

   ```
   # <topic>

   **Timeline**
   - <date> (<slug>): <one-line what happened>
   - ...

   **Decisions made**
   - ... (with source slug)

   **Current state / open threads**
   - ... (with source slug)

   **Recent quotes**
   - > "..." — Speaker, <slug> [MM:SS]
   ```

5. **Anchor everything.** Every factual claim in the digest must trace back to a specific meeting slug. If the user wants to verify, they jump straight there.

## Don'ts

- **Don't synthesize beyond evidence.** "It seems like the team is leaning toward X" is only ok if multiple transcripts actually show that leaning — cite them.
- **Don't include meetings where the topic was only tangentially mentioned** (one passing reference in a 45-min meeting). Quality over recall.
- **Don't forget to check `speakers.json`** — if a name in the transcripts is actually `speaker_0`, surfacing that map helps the user understand who said what.
