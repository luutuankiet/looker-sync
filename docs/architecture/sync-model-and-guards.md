---
title: Sync model and guards
covers: how pull, push, status and switch decide what to write, which guard refuses what, and where in looker_sync.py each lives
verified: 2026-10-09
---

# Sync model and guards

Everything is in `scripts/looker_sync.py`; tests are in `scripts/test_looker_sync.py` and
run the CLI as a subprocess against a fake Looker.

- **State.** `.looker-sync/state.json` holds the SHA-256 of each file's Looker content at the
  last successful pull or push. A file is *changed locally* when its hash differs from that,
  and *changed in Looker* when Looker's hash differs. `plan()` turns that into one kind per path.
- **Session.** Every command logs in fresh, sets the workspace mode, reads it back, prints
  the Looker user, and in dev mode aborts when Looker's branch differs from the config
  (`Session.__init__`). The branch belongs to the user, not the token; see
  [looker-file-api-behaviour](../reference/looker-file-api-behaviour.md).
- **Push (`do_push`).** Refuses in prod mode and when a file changed in Looker (unless
  `--force`). Writes new files before changed ones, reads each back and records it at once,
  so a push that stops partway leaves state matching Looker. Deletes only with `--delete`.
  Validates last.
- **Pull (`do_pull`).** Never deletes local files; refuses to overwrite unpushed local changes.
- **Switch (`cmd_switch`).** Refuses while Looker has uncommitted files; asks what to do with
  unpushed local changes (push, backup, cancel); checks out by name only, never `ref`.
- **Never done by the tool:** commit, deploy, reset, create a branch.

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.
