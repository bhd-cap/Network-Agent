# Arista EOS: making changes safely (config sessions, rollback, checkpoints)

## Config session with a commit timer (preferred for remote changes)
```
configure session CHG1234
   ... configuration ...
   show session-config diffs          ! review exactly what will change
   commit timer 00:05:00              ! apply now, auto-rollback in 5 minutes unless confirmed
```
Verify the change, then confirm:
```
configure session CHG1234 commit
```
If the timer expires (for example you lost access) EOS restores the previous configuration.
To discard a session without applying: `abort` inside the session.

Useful:
- `show configuration sessions detail` - sessions and their state (pending, pendingCommitTimer, completed).
- `show session-config named CHG1234` - the full candidate.

## Replace or roll back
- `configure replace flash:<file>` - atomically replace the running config with a file.
- `show archive` / config checkpoints: `configure checkpoint save <name>` and
  `configure checkpoint restore <name>`.
- `rollback clean-config` wipes to factory config - never use as an "undo".

## Persist
- `write memory` (or `copy running-config startup-config`). A session commit changes running-config only.

## Compare
- `show running-config diffs` - running vs startup.
- `diff startup-config running-config`
- `show running-config section <keyword>` and `show running-config | json` for automation.

## eAPI / automation notes
- eAPI must be HTTPS only: `management api http-commands` / `protocol https` / `no shutdown`.
- eAPI supports sessions: send `configure session X`, the changes, then `commit` in one request for an
  all-or-nothing change.
