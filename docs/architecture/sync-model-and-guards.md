---
title: Sync model and guards
covers: how pull, push, status and switch decide what to write, which guard refuses what, and where in looker_sync.py each lives
verified: 2026-10-24
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
