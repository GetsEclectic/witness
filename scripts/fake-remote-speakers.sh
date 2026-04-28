#!/usr/bin/env bash
# Simulate two remote meeting participants by having espeak-ng play through
# the default audio sink. Anything coming out of the speakers gets captured
# on the recorder's system channel (ch1), and Deepgram diarizes it.
#
# How to use:
#   1. Start a solo Google Meet (so the daemon detects + starts recording).
#   2. In another terminal, run: bash scripts/fake-remote-speakers.sh
#   3. Talk over it occasionally on the mic to seed your own utterances on ch0.
#   4. When done, leave the Meet. witness runs automatically.
#
# Result: transcript.md should show interleaved local mic + Spk 0 + Spk 1.
# speakers.json will assign two unknown_<hash> ids that you can rename
# via `witness relabel <slug> unknown_<hash> "Whoever"`.

set -euo pipefail

# Two distinct espeak-ng voices — clearly different so Deepgram diarizes
# them as separate speakers without much chance of clustering them together.
A_VOICE="en-us+m3"   # American male
B_VOICE="en-gb+f3"   # British female

a() { espeak-ng -v "$A_VOICE" -s 160 "$1"; sleep 1; }
b() { espeak-ng -v "$B_VOICE" -s 165 "$1"; sleep 1; }
pause() { sleep "${1:-3}"; }

a "Hey, thanks for jumping on. Quick sync on the rate limiter migration. How is it going?"
pause 5
b "From my side, I just want to confirm we are keeping the old token bucket behind a feature flag for now."
pause 4
a "Yes, we are. The plan is to rip it out at the end of next sprint, assuming nothing breaks."
pause 4
b "Sounds reasonable. Should we book a post mortem slot proactively?"
pause 4
a "Good idea. Can you put thirty minutes on the calendar for Thursday?"
pause 5
b "While we are talking budgets — do we have headcount approved for a staff engineer role this half?"
pause 5
a "Not confirmed yet. We are waiting on finance to finalize the Q two budget."
pause 4
b "Alright, let us pick this back up next week. Thanks for the update."
pause 2
a "Same. Talk soon."

echo "done — leave the Meet whenever you're ready."
