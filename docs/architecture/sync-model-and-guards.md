---
title: Sync model and guards
covers: how pull, push, status and switch decide what to write, which guard refuses what, and where in looker_sync.py each lives
verified: 2026-11-08
---

# Sync model and guards

Everything is in `src/looker_sync/cli.py`; tests are in `scripts/test_looker_sync.py` and
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
- **Never done by the tool:** commit, deploy, reset, create a branch (in Looker or git; see the git section below).

Exit codes: 0 ok, 1 Looker or network error, 2 refused by a guard, 3 validation errors.

## Git-aware folders (verified 2026-10-24)

The local git layer sits beside the Looker client in `cli.py`: `run_git` (the only place a
git process starts; `git_in_scope` refuses anything outside the scope of
[ADR 0003](../adr/0003-never-touch-lookers-git-and-a-narrow-local-git-scope.md)), `LocalGit`
(reads), and the switch helpers `git_can_follow`, `git_stash`, `git_checkout`. Without
`git_remote` in the config none of it runs; `looks_like_git` is pure Python, so an unlinked
folder starts no git process at all.

- **Linking (`link_folder`).** `git_remote` is a remote *name*, never a URL, so SSH aliases
  and HTTPS clones work alike. `link [--remote NAME]` (default `origin`); `init --git-remote
  NAME` links or refuses; `init --no-git` skips; without either, `init` asks on a terminal and
  otherwise tries `origin`, continuing unlinked with the reason if that is refused.
  `not_linkable` refuses a project without git, with no remote, or with a Looker-hosted bare
  repository (a `git_remote_url` that is not a URL).
- **Commit check (`commit_check`, run by `open_session` on every command in a linked folder,
  and by `link`).** `git fetch <remote>` (a failure only warns), then the commit Looker's
  branch reports as `ref`, else `remote_ref`, must exist locally. If neither does and Looker's
  branch is ahead of its remote, it warns; otherwise it refuses and prints Looker's remote URL
  next to the local remote's (user info masked by `safe_url`). No URL is ever compared. It
  proves the commit exists in the clone, not which remote it came from (until 0.2.1; see
  "Commit check: the linked remote" below).
- **Switch in a linked folder (`cmd_switch`).** Existing guards first (uncommitted files in
  Looker still refuse, `--force` does not override that); `git_can_follow` refuses a target
  that exists only locally ("push it to the remote first") or only in Looker. Then: Looker
  moves; `git_stash` runs `stash push --include-untracked` named `looker-sync <from>
  <timestamp>`, excluding the tool's own paths (`.looker-sync/`, an env file inside the
  folder) unless git already ignores them, because git 2.43 fails `stash push` halfway when an
  exclude pathspec names an ignored path; `git_checkout` checks out the target (creating it
  with `--track` from `<remote>/<target>`) and fast-forwards to Looker's `ref` only when that
  is a pure fast-forward, else warns. The file-copy backup is used only for synced files the
  stash could not take (git-ignored ones). Then the local files are listed again, after the
  checkout, and Looker's files are mirrored. `switch` says "nothing to switch" only when
  config, Looker and git agree, so it also repairs git drift. Prod mode uses
  `git_production_branch_name`. `--force` = `--local-changes backup --yes`.
- **Drift guard (`drift_guard`).** Git on another branch than the config branch (or a
  detached HEAD): `status` warns, `pull` and `push` refuse (exit 2), both printing
  `looker-sync switch --branch <config branch> --force`. `init`'s first pull hits it too, so a
  clone linked while off Looker's branch is never written over.
- **Read-only push refusal (`do_push`).** `push` refuses when Looker reports the branch
  `readonly` (a colleague's personal branch); `switch` still allows it, because a review only
  reads. This applies in unlinked folders too. It was NOT verified live with two users: the
  fake serves the flag, and live showed only that the production branch reads `readonly: true`
  in dev mode.

## Commit check: the linked remote (verified 2026-10-24, 0.2.1)

Since 0.2.1 `commit_check` also proves which remote the commit came from. A commit counts
only when it exists in the clone (`cat-file -e`) AND `LocalGit.on_remote` finds a ref under
`refs/remotes/<git_remote>/` that contains it (`for-each-ref --count=1 --contains <sha>
refs/remotes/<remote>/`, against what the fetch just brought, or what was fetched before when
the fetch failed). Containment is checked across all of the remote's branches, not only
Looker's branch, so a new Looker branch with no commits of its own (its commit sits on the
production branch) still passes. A clone with two remotes linked to the one that lacks
Looker's commit now refuses with "is in this clone but on no branch of remote" and both URLs;
"Looker is ahead" still only warns and a failed fetch still only warns. `git_in_scope` allows
`for-each-ref` only with `--contains` (ADR 0003, amended).

## Fast push: what push reads and when it fetches (verified 2026-11-08)

- **Push reads only the files it will write or delete (`push_reads`, `do_push`).** Push lists
  Looker's files once, then reads only those it may change: a listed file whose local hash
  differs from `state.json` (a new local file that Looker already lists included), or a file
  deleted locally that Looker still lists. A push with nothing changed reads no file; a push
  of one changed file reads that one file before writing it, then reads it back. Every file
  push writes or deletes is still read first, so the "changed in Looker" refusal, `--force`,
  the byte-for-byte read-back and validation are unchanged for those files.
- **A file changed only in Looker no longer blocks push.** If a file is unchanged locally,
  push leaves it alone whatever Looker holds, so it is not read and cannot refuse the push.
  Push plans it as unchanged in Looker. A file changed on both sides is still refused, and a
  file deleted in Looker (in `state.json`, no longer listed) is still refused from the listing
  alone. `status` and `pull` still read every file and report Looker's changes.
- **Push skips `git fetch` when it is not needed (`commit_check(fetch=False)`).** In a linked
  folder, push first checks Looker's `ref` and `remote_ref` against the clone's remote
  branches as last fetched. If one is already in the clone and on a branch of the linked
  remote, the check passes without fetching: a fetch without `--prune` only adds refs, so it
  could not turn that pass into a refusal. Otherwise push fetches and runs the full check.
  `status`, `switch` and `link` still fetch every time (`git_can_follow` needs fresh tracking
  refs).
- **`push --no-validate`** skips Looker validation and prints `validation: skipped
  (--no-validate)`; the read-back still runs.
- **`push --verbose`** prints one `timing: <phase> <seconds>` line per phase on stderr (session,
  commit check, drift guard, local scan, file list, remote reads, write and read-back,
  validate, total) and shows info-severity validation lines. Without it, push hides those
  lines and says how many it hid: `(N info line(s) hidden; --verbose shows them)`.
- **Measured live** (Linux, sandbox, median of 5): a push with no change went from 8.2 s to
  2.9 s, a push of one changed file from 10.2 s to 5.3 s. Session setup is now most of a
  no-change push.

## Watch (verified 2026-11-08)

`cmd_watch` refuses in prod mode, on a read-only branch and on git drift. `WatchCore` is the
scanner and a one-slot worker, with no network. `WatchNet` adds the Looker side.

- **Reconcile, not events.** Each scan (default every 50 ms) compares file stats with the last
  scan and derives the pending work from disk versus the hashes last synced. There is no queue,
  so after a stall only the latest content is pushed.
- **Settle.** 150 ms for an in-place save; none for an atomic rename; 0.5 s in a burst (5 or more
  paths at once, or a repeat within 1 s); 3 s at most, so a noisy file cannot starve the others.
- **Order for one file.** The conflict read starts when the change is first seen. Then the syntax
  gate (`gate.check`), then `WatchNet.push_one`, which prints `ack` once Looker accepts the
  write. A separate thread reads the file back and prints `verified` or `MISMATCH`; only a
  verified file is recorded in `state.json`.
- **Holds apply to the whole batch** for a syntax error (`_gatekeep`), a git operation in
  progress (`GIT_BUSY`, or HEAD moving), an unconfirmed branch (30 s), and more than 200 changed
  files. One-shot `push` runs no syntax gate, so it is the way past a false hold.
- **Holds apply per file** for a change in Looker (see
  [ADR 0004](../adr/0004-hold-a-file-changed-in-looker.md)), a move, a likely copy, a file still
  being written after the 3 s limit, and a file that cannot be read.
- **Never deletes.** A local delete or a move is reported and left for `push --delete`.
- **Pauses, never exits.** Reasons: Looker's branch or mode changed, read-only branch, refused
  session, project folder gone. A background check runs every 5 s (`WatchNet.branch_tick`). If it
  finds another branch after writes since the last check, an `error` line says those writes may
  have landed there.
- **Not tested live:** a real branch switch in the Looker IDE. The live check changed the name
  the watcher expects and left Looker's branch alone; the fake Looker covers its own branch
  changing (`WatchNetBranchTest`).
