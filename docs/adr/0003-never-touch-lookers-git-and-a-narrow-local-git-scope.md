# Never touch Looker's git state; touch a linked clone only through a narrow, checked scope

Supersedes [0001](0001-read-and-write-files-only.md), which said the tool never touches git
state at all.

**Looker side, unchanged.** `looker-sync` never commits, deploys, resets or creates a branch
in Looker, and never sends `ref` to the branch endpoint (it hard-resets and force-pushes). It
checks out an existing branch by name, and nothing else changes Looker's git.

**Local side, new.** A folder can be *linked* to a git remote of the clone it lives in (config
key `git_remote`, a remote name such as `origin`). In a linked folder the tool may run only:
reads (`rev-parse`, `symbolic-ref`, `status`, `cat-file`, `merge-base`, `remote get-url`),
`fetch`, `stash push`, `checkout` (including creating a branch that tracks
`<remote>/<branch>`), and `merge --ff-only`. Never commit, push, pull, reset, rebase, `clean`
or `stash pop`. Every git process goes through one runner, `run_git` in
`src/looker_sync/cli.py`, which refuses anything outside that list (`git_in_scope`), so the
rule can be checked by reading one function. An unlinked folder starts no git process.

**Why.** A looker-sync folder is often also a git clone, and the two follow different
branches: Looker's dev workspace and git's checkout. When they drift, `git diff` shows another
branch's files as edits and a `pull` writes one branch's files onto another branch's
checkout, with no error. `switch` therefore has to move both sides together, which needs a
checkout, a stash for whatever was dirty, and a fast-forward so the mirrored files are not
fake edits. Each of those is either recoverable (a stash is never popped by the tool, a
checkout leaves history alone) or a pure fast-forward to a commit the remote already has.
Everything that rewrites or publishes history stays with the user, in git or the Looker IDE.

**Cost.** The user still commits, pushes and pops stashes by hand, and a diverged local
branch is only warned about, never repaired.

**Amended 2026-10-24 (0.2.1).** One read joins the scope: `for-each-ref`, allowed only with
`--contains`. The commit check uses it to prove Looker's commit is on a branch of the linked
remote (`refs/remotes/<remote>/`), not merely somewhere in the clone, so a clone with two
remotes cannot be linked to the wrong one. It is a pure read; `branch --contains` was not
chosen because `branch` also deletes and renames, which would need a narrower check.
