---
name: looker-sync
description: Move LookML between a local folder and your own Looker dev workspace with the looker-sync CLI (pull, status, push with byte-for-byte read-back and validation, switch, watch for continuous sync). Use before editing any LookML that must reach Looker, before validating a Looker project, when mounting a Looker project into a new folder, and when Looker and the local files might disagree.
---

# looker-sync

`looker-sync` is the only path from local LookML to Looker. It talks to the Looker API with
your own key. It never commits, deploys, resets or creates branches: Looker's API has no
commit or push endpoint, so committing stays with git, and the rest stay clicks in the Looker
IDE. Agents cannot commit LookML through this tool.

```
uvx looker-sync [-C <project folder>] [--env-file <path>] <command>
```

`-C` defaults to the current folder. The env file holds `LOOKER_URL`, `LOOKER_CLIENT_ID`
and `LOOKER_CLIENT_SECRET`. It is found as `--env-file`, then `LOOKER_SYNC_ENV`, then the
`env_file` in `.looker-sync/config.json`, then `.env` in the project folder. Never open it
yourself. `looker-sync --help` is the short reference; `looker-sync skill` prints this guide.

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
  In a linked folder (below) the backup is a `git stash` instead, and git moves too.
- Only `*.lkml` and `*.dashboard.lookml` sync by default (`include`/`exclude` in the
  config). `.env`, skills, editor settings and `.looker-sync/` never reach Looker.
- The Looker MCP write tools are not an alternative: they run in production mode under a
  separate login and cannot see dev files. MCP read and query tools are fine.

## Mounting a project, including from a blank folder

```
mkdir my-project && cd my-project
uvx looker-sync init --project <name> [--branch <b>]
```

In a folder with no credentials, `init` writes a `.env` template (owner-only permissions),
adds `.env` and `.looker-sync/` to `.gitignore`, and exits with code 2 telling you what to
fill in. The user fills `LOOKER_URL`, `LOOKER_CLIENT_ID` and `LOOKER_CLIENT_SECRET`; run the
same `init` command again and it writes `.looker-sync/config.json` and pulls. It never
overwrites a filled `.env` or an existing config. On a terminal it asks for the project and
whether to pull; `--no-pull` writes metadata only.

`init` refuses if the Looker dev workspace is on a different branch than `--branch` (it
never switches), and its first pull refuses to overwrite local files that differ from
Looker (`--force` takes Looker's copy). Mount into an empty folder and compare by hand when
in doubt: a folder kept by another sync tool can be stale.

## Git: a folder that is also a clone

Link the folder to a git remote of its clone and looker-sync keeps the git branch and the
Looker branch together. Unlinked folders behave as above and never run git.

- **Link.** `link [--remote NAME]` (default `origin`; a remote *name*, never a URL), or
  `init --git-remote NAME`; `init --no-git` skips. Without either, `init` asks on a terminal,
  and without one it tries `origin` and carries on unlinked if that is refused, saying why.
  A project without git, without a remote, or with a Looker-hosted bare repository cannot be
  linked ("there is nothing to clone").
- **Commit check, every command.** `git fetch`, then the commit Looker's branch is on must be
  in the clone and on a branch of the linked remote (a commit fetched from another remote does
  not count). A failed fetch only warns. If Looker has commits not pushed to the remote yet,
  it warns. Otherwise it refuses with "It looks like a clone of a different repository" and
  prints `Looker's remote:` next to `remote 'origin':` (with "is in this clone but on no branch
  of remote" when the clone has the commit from another remote). Fix the remote with
  `git remote set-url`, or link another one.
- **Switch.** `switch --branch X --force` is the one command for a review: Looker moves to X;
  a dirty tree (untracked files too, never `.looker-sync/` or the env file) is stashed as
  `looker-sync <from-branch> <timestamp>`; git checks out X (created from `<remote>/X` if
  new) and fast-forwards to Looker's commit when that is a pure fast-forward (diverged or
  ahead: a warning, the branch is left alone); then Looker's files are mirrored. `--force` is
  exactly `--local-changes backup --yes`; it does not override the uncommitted-in-Looker
  refusal. A branch only in your clone is refused: "exists only in this clone; push it to the
  remote first". Prod mode checks out Looker's production branch.
- **The stash.** The switch prints the stash name and how to get it back, for example
  `To get them back: looker-sync switch --branch <from> --force, find it with git stash list,
  then git stash pop`. If git had drifted, it says `git checkout <from>` instead.
  looker-sync never pops a stash; popping is the user's call.
- **Drift.** After a hand `git checkout`, `status` prints
  `warning: git is on 'B', not 'A'; pull and push refuse until it is back. Fix:` and `pull`,
  `push` (and `init`'s first pull) refuse with exit 2 and the same fix line:
  `looker-sync switch --branch A --force`. A detached HEAD is drift too. Run the fix line;
  it repairs the git side even when Looker is already on A.
- **Read-only branch.** `push` refuses when Looker reports the branch read-only for you, as a
  colleague's personal branch is: "review it, then switch to a branch of your own to push".
  Linked or not.
- **Never:** commit, push, pull, reset, rebase, `git clean` or `stash pop`. Allowed: reads,
  `fetch`, `stash push`, `checkout` and `merge --ff-only`. Commit and pull with git yourself;
  pull Looker from its remote in the Looker IDE.

## Fast push flags

`push` reads from Looker only the files it will write or delete, so a file changed only in
Looker no longer refuses a push that leaves it alone; `status` and `pull` still report it. In a
linked folder `push` and `watch` run `git fetch` only when Looker's commit is not already on a
branch of the remote as last fetched.

- `push --no-validate` skips validation and prints `validation: skipped (--no-validate)`. The
  read-back still runs.
- `push --verbose` prints `timing: <phase> <seconds>` lines on stderr and shows info-severity
  validation lines. Without it they are hidden and counted:
  `(N info line(s) hidden; --verbose shows them)`.

## Watch: continuous sync

```
uvx looker-sync watch [--validate] [--verbose] [--poll MS] [--force] [--no-delete]
```

Writes each save to the dev workspace, usually in under a second, and mirrors local deletes and
renames, until Ctrl-C. Validation is off unless `--validate`, which runs it after 2 s without a
write. `--poll` is the scan interval in ms (default 50, at least 10). `--force` overwrites or
deletes files changed in Looker; it needs a stated reason, like `push --force`. `--no-delete`
only reports local deletes and renames.

Read the output. One line per event: time, kind, then the file or the reason.

| kind | meaning | what to do |
|---|---|---|
| `ack` | Looker accepted the write | carry on |
| `verified` | read-back matched, recorded as synced | carry on |
| `MISMATCH` | Looker holds different bytes than were sent | check it in the Looker IDE, then `pull` or `push --force` |
| `deleted` | Looker no longer holds this file or folder | carry on, unless you did not mean to delete it |
| `held` | not written or deleted; the line gives the reason | read the reason, see below |
| `released` | an earlier hold no longer applies | carry on |
| `paused` / `resumed` | nothing is written until the reason clears | read the reason; a branch change is for the user |
| `notice` / `error` | information, or a failure being retried | read it |

- **`ack` is not proof.** Wait for `verified` before you rely on the bytes being in Looker.
- **Syntax gate.** One `.lkml` file that does not parse holds every pending file, and the line
  names the file and the reason. Fix it and they all go. `.dashboard.lookml` files are not
  checked. **One-shot `push` runs no syntax gate**, so it is the escape hatch for a hold you are
  sure is wrong; Looker validation still runs after it.
- **Changed in Looker.** The file is held, not overwritten, and other files carry on. It is
  held again each time the local bytes change while Looker's copy still differs. `pull` takes
  Looker's copy if you have not touched the file; otherwise report it.
- **Deletes by default.** A synced file deleted locally while `watch` runs is deleted in Looker
  after 1.5 s, with a `deleted` line. A rename or move writes and verifies the new path, then
  deletes the old one; a failed write leaves both. Copies are written like any new file. So
  **do not `rm` a file in the watched folder unless you mean to delete it in Looker.** Uncommitted
  work there has no history. Files that exist only in Looker, and files already missing at start,
  are never touched; use `push --delete` for those, which needs a stated reason.
- **Folders.** A folder that vanishes locally is removed in Looker once its synced files are gone,
  topmost folder only, with a `deleted` line ending `(folder)`. Looker's folder delete is
  recursive, so a listing just before the call must show no file under it. Otherwise the folder
  is left and reported once in a `notice`, and not retried unless it reappears locally.
- **Holds apply to deletes too:** prod mode, paused or wrong branch, project folder gone, an
  unreadable folder, a git operation, the syntax gate, and the 200 file ceiling (writes plus
  deletes). A file changed in Looker is held, not deleted, unless `--force`.
- **Git.** A path that vanishes during a git operation, or within 1.5 s of a ref move, is never
  deleted; a `held` line points at `push --delete`. **`git clean` takes no lock and moves no ref,
  so it looks like `rm` and WILL delete.** Stop `watch` before running it.
- **`--no-delete`** is the older behaviour: local deletes and moves are reported and left for
  `push --delete`, a new file that looks like a copy of another is held until edited or pushed,
  and no file or folder is deleted.
- **Holds with no action needed:** a git operation in progress, and a file still being written.
  More than 200 changed files at once are held for a one-shot `push`.
- **Branch.** Checked every 5 s in the background, and every write is held if the branch has not
  been confirmed for 30 s. A branch switched in the IDE between two checks can take a write: the
  next check prints an `error` line saying writes may have landed there, and pauses. Stop and
  tell the user. Only `switch` moves branches.
- **Exit codes.** 0 on Ctrl-C, 2 if refused at start, 1 if Looker cannot be reached at start.
  After that, errors pause or retry; the process does not exit, so a script cannot rely on the
  exit code to see a problem. Read the lines.

## Committing

`looker-sync` does not commit. `status` prints how many commits the dev branch is ahead of
and behind its remote; commit and push with git from the Looker IDE or a clone of the
remote. Looker has no commit or push endpoint in its API, and the official Looker VS Code
extension (1.0.0) does not use one either: it commits through local git.

## Tests

From a source checkout, `python3 -m unittest discover -s scripts -p 'test_looker_sync.py'`
runs the CLI against a fake Looker. Run it after any change to the tool.
