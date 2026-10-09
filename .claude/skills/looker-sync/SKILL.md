---
name: looker-sync
description: Move LookML between a local folder and your own Looker dev workspace with scripts/looker_sync.py (pull, status, push with byte-for-byte read-back and validation, switch). Use before editing any LookML that must reach Looker, before validating a Looker project, when mounting a Looker project into a new folder, and when Looker and the local files might disagree.
---

# looker-sync

`scripts/looker_sync.py` is the only path from local LookML to Looker. It talks to the
Looker API with your own key, standard library only, nothing to install. It never commits,
deploys, resets or creates branches: those stay clicks in the Looker IDE.

```
python3 scripts/looker_sync.py -C <project folder> [--env-file <path>] <command>
```

`-C` defaults to the current folder. The env file holds `LOOKER_URL`, `LOOKER_CLIENT_ID`
and `LOOKER_CLIENT_SECRET`; pass its path and let the tool read it. Never open it yourself.
After `init` the path is remembered in the folder's `.looker-sync/config.json`.

## The loop

1. `status`: what differs. Nothing is written.
2. Edit the files with your normal tools.
3. `push --dry-run`, then `push`. Push writes local changes, reads every file back, compares
   bytes, then runs LookML validation and prints errors as `file:line  message`.
4. Fix, push again, until exit 0.

| exit | meaning | what to do |
|---|---|---|
| 0 | ok | carry on |
| 1 | Looker or network error, or a read-back mismatch | read the message; do not retry blindly |
| 2 | refused by a guard | read the reason; it names the files |
| 3 | pushed and verified, but validation has errors | fix them; check whether they were there before your push |

**Read the first line of every run.** It names the Looker user, project, mode and branch.
If the user is not the developer you are working for, stop: a key for another user writes
to that user's dev workspace, which the developer never sees.

## Guards, and what each refusal means

- **Branch mismatch.** Looker's dev workspace is on another branch than the folder's config.
  Someone switched in the IDE. Stop and tell the user; only `switch` moves branches.
- **Changed in Looker since the last sync** (push). Someone edited in the Looker IDE. Run
  `pull` to take their copy if you have not touched that file; otherwise report it. Use
  `push --force` only when the user has said local wins.
- **Pull would overwrite unpushed local changes.** Push your work first. `pull --force`
  throws it away.
- **Push refused in prod mode.** Production is read-only by design.
- **One key, one branch at a time.** The checked-out branch belongs to the Looker user, not
  to a token: a second client or folder on another branch hits the branch-mismatch guard, and
  only `switch` moves everyone. Do not run two branches in parallel on one key.

## Rules

- A new folder is created for you. A push writes new files before changed ones and records
  each file as soon as it is written and read back. If it stops partway it prints `written:`
  and `not written:`; just push again, no `--force` is needed.
- `--force` and `--delete` need a stated reason in your reply. Files that exist only in
  Looker are listed by every push and deleted only with `--delete`.
- `pull` never deletes local files, and keeps a file you deleted locally deleted.
- `switch` moves the user's whole dev workspace, browser IDE included. Leave it to the user
  unless they asked. It refuses while any Looker file is uncommitted, and without a terminal
  it needs `--yes` plus `--local-changes push|backup|cancel` when local work is unpushed.
  Backups land in `.looker-sync/backups/<branch>-<timestamp>/`.
- Only `*.lkml` and `*.dashboard.lookml` sync by default (`include`/`exclude` in the
  config). `.env`, skills, editor settings and `.looker-sync/` never reach Looker.
- The Looker MCP write tools are not an alternative: they run in production mode under a
  separate login and cannot see dev files. MCP read and query tools are fine.

## Mounting a project

```
python3 scripts/looker_sync.py -C <empty folder> --env-file <env> init --project <name> [--branch <b>]
```

`init` refuses if the Looker dev workspace is on a different branch than `--branch` (it
never switches), and its first pull refuses to overwrite local files that differ from
Looker. Mount into an empty folder and compare by hand when in doubt: a folder kept by another
sync tool can be stale.

## Tests

`python3 -m unittest discover -s scripts -p 'test_looker_sync.py'` runs the CLI against a
fake Looker. Run it after any change to the tool.
