# Captured panes

Verbatim `tmux capture-pane` output of real Claude Code 2.1.276 sessions
launched by Maestro, with the machine's user and paths replaced. They are the
ground truth for `maestro.claude`'s state detection: when a new Claude Code
changes its interface and detection breaks, capture the new screen here
(`tmux capture-pane -p -t <pane> -S -60 > tests/fixtures/<state>.txt`) and
adjust the patterns against it rather than guessing.

| File | What it shows |
|---|---|
| `worker_after_launch.txt` | a prompt pasted into a fresh session, before the spinner appears |
| `worker1_processing.txt` | a turn running |
| `worker1_completed.txt` | one finished turn |
| `worker2_processing.txt` | a second turn running, with the first answer above it |
| `worker2_completed.txt` | two finished turns — the "is this answer new?" case |
| `worker_two_turns_full.txt` | the same session with its whole scrollback |
| `rewind_dialog.txt` | Claude's Rewind menu, opened by a second Escape on an idle prompt |

States that are awkward to catch live (a live spinner, an open question) are
synthesised from these in the tests.
