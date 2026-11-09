# Watch deletes by default what you delete locally during the run

**Decision.** With no flag, `watch` mirrors local deletes to your Looker dev workspace. A synced
file that vanishes while `watch` runs is deleted in Looker. A rename or move writes and verifies
the new path, then deletes the old one. A folder that vanishes locally is removed in Looker once
all its synced files are gone. `watch --no-delete` restores the older behaviour: deletes, moves
and likely copies are only reported, and no file or folder is deleted. Every other way to delete
still needs `push --delete`.

The narrower rule replaces "never delete without `--delete`" for `watch` only. The logic is in
`WatchCore._plan_deletes`, `WatchCore._plan_folders`, `WatchNet.delete_one` and
`WatchNet.delete_folder` in `src/looker_sync/cli.py`.

**Why.** `watch` exists so that development can happen entirely in a local editor, with Looker
following along. Local work includes deleting, renaming and moving files and folders. A watch
that writes but cannot delete leaves old files in Looker. Those files still parse, still define
views and Explores, and clash with the renamed ones, so the owner of the project treated a watch
that could not delete as unusable. Once the delete is the person's own recent action on their
own branch, in a workspace `watch` was started on, the case for a separate confirmation is weak.

**The guards.**

- **Only what this run saw vanish.** A file already missing when `watch` starts is reported, not
  deleted. A file that exists only in Looker is never a candidate.
- **A grace period.** A path must stay absent for 1.5 s and pass a fresh `lstat` before the delete
  is sent, so an atomic save by an editor never looks like a delete.
- **Git.** A path that vanishes during git activity, or within 1.5 s of any ref move, is never
  deleted. It gets a notice pointing at `push --delete`. A `git init` after `watch` starts is
  picked up, so its later checkouts are guarded too.
- **A file changed in Looker is held,** not deleted, unless `--force`. This is the check from
  [ADR 0004](0004-hold-a-file-changed-in-looker.md).
- **Rename order.** The old path is deleted only after the new path is read back and matches. A
  failed write leaves both paths in Looker, never neither.
- **Whole-batch holds apply to deletes too:** prod mode, paused or wrong branch, project folder
  gone, an unreadable folder, git busy, the syntax gate, and the 200 file ceiling, which counts
  writes plus deletes.
- **Folders.** Looker's folder delete is recursive, so it gets the most guarding. Only the topmost
  vanished folder is removed. It must have stayed absent for the grace period with git quiet, and
  no synced file may remain under it, so a held or failed file blocks it. A fresh file listing
  just before the call must show no file at all under the folder. If it shows one, the folder is
  left and reported once, and it is not tried again unless it reappears locally.

**Remaining risks.**

- **A mistaken local `rm` deletes uncommitted work in Looker's dev workspace.** Uncommitted work
  there has no history to recover it from. If the same files are not in git or on disk elsewhere,
  they are lost. The guards above cannot tell a mistake from an intent.
- **`git clean` looks like `rm` and WILL delete.** It takes no lock and moves no ref, so nothing
  ties the vanished files to git. Stop `watch`, or start it with `--no-delete`, before running it.
- **A file created in Looker during one round trip can be destroyed.** A file created between the
  file listing and the recursive folder delete goes with the folder. The API has no conditional
  delete, so this gap cannot be closed from the client. It is one round trip wide and needs
  someone to create a file inside a folder that was just removed locally.

**Alternatives considered.**

- **An opt-in `--delete` flag on `watch`.** Offered and refused by the owner. Forgetting the flag
  gives the same stale-file problem as no delete at all, and a long-running development tool
  should do the normal thing without being told. `--no-delete` is the opt-out instead.
- **Leave deletes for `push --delete`.** The previous behaviour. It breaks the flow on every
  rename, and the new path of a move did not reach Looker either.
- **Remove folders with no listing check.** Simpler and one call fewer, but the call is recursive
  and would destroy any file only Looker has under that folder.
- **Leave empty folders behind.** Safe, but they pile up in the Looker IDE after every folder
  rename, which defeats the point of working locally.

**Cost.** A delete arrives about 1.5 s after the local action because of the grace period. The
default now carries the risks above, and the safer behaviour needs a flag.
