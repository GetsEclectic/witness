# Claude Code skills for witness

Four skills that let Claude Code answer questions about your recorded meetings by reading directly from `$WITNESS_MEETINGS_DIR` (default `~/meetings/`):

- **meetings-search** — keyword/phrase search across transcripts and summaries
- **meetings-on-topic** — narrative digest on a person, project, or theme
- **meetings-action-items** — aggregate open action items
- **meetings-week** — weekly recap

## Install

Symlink each skill into your Claude Code skills directory:

```sh
for s in skills/meetings-*; do
  ln -s "$PWD/$s" ~/.claude/skills/
done
```

(Or copy with `cp -r` if you'd rather a snapshot.)

If `$WITNESS_MEETINGS_DIR` isn't `~/meetings/`, export it in your shell profile so Claude Code subprocesses see the right path:

```sh
echo 'export WITNESS_MEETINGS_DIR=/path/to/your/meetings' >> ~/.zshrc
```

Restart Claude Code (`/quit` then relaunch) and the skills should appear in `/skill`.

## Notes

- The skills read from the filesystem, so no API keys are needed beyond what witness itself uses.
- They assume the file layout produced by the witness pipeline (`transcript.md`, `summary.md`, `metadata.json`, `speakers.json`). If you're hand-editing meeting folders, mirror that layout.
- Each `SKILL.md` has its own description that Claude Code uses to decide when to invoke the skill — they're written to trigger on natural phrasings like "what did I commit to" or "catch me up on X".
