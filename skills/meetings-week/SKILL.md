---
name: meetings-week
description: Produce a weekly digest of recorded meetings — who the user met with, key decisions, and action items. Use for "what did I do this week", "weekly recap", or Monday planning.
---

# meetings-week

Synthesizes a week's worth of meeting summaries into a single digest. Useful for Friday retros, Monday planning, or catching the user up after they've been offline.

Meetings live at `$WITNESS_MEETINGS_DIR/<slug>/` (default `~/meetings/<slug>/`).

## How to use this skill

1. **Determine the window.** Default is the current ISO week (Mon 00:00 → now). If the user says "last week", shift one week earlier. Folder slugs use ISO dates, so a glob like `"$WITNESS_MEETINGS_DIR"/<YYYY-MM-DD>*` for the Monday onward gives you the week.

2. **Collect summaries.** `ls "$WITNESS_MEETINGS_DIR"/<prefix>*/summary.md` — read each one.

3. **Structure the digest:**

   ```
   # Week of <Mon> – <Fri>

   **Meetings** (<N>)
   - <slug>: <calendar title> · with <attendees>

   **Top themes**
   - 2–4 bullets naming the recurring topics

   **Decisions**
   - ... (with source slug)

   **Action items still open (yours)**
   - [ ] ... — from <slug>

   **What to watch next week**
   - pull from `## Open questions` sections across the week
   ```

4. **Keep it tight.** The user reads this for situational awareness, not comprehensiveness. If every meeting had the same flavor ("1:1 syncs, nothing notable"), say so in one line instead of listing each.

## Don'ts

- Don't include meetings the user didn't have a clear role in (e.g. ones where their action items are empty and they didn't speak — they probably dropped in just to listen).
- Don't skip meetings without `summary.md` — flag them by slug so the user knows the pipeline has gaps.
- Don't make up a "team mood" or "sentiment" read. Stick to what decisions and actions are documented.
