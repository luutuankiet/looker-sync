# Hold a file changed in Looker; never overwrite it without --force

**Decision.** When `watch` is about to write a file and Looker's copy is not the one last
synced, it does not write. It prints a `held` line naming the file and carries on with every
other file. The hold lasts until the local bytes change, and then the check runs again. `pull`
takes Looker's copy; `watch --force` or `push --force` overwrites it. A file deleted in Looker
is held the same way. If Looker already holds exactly the local bytes, nothing is written and
the file counts as synced.

The check is in `WatchNet.push_one` in `src/looker_sync/cli.py`: the file's hash in Looker is
compared with the hash last known for it.

**Why.** `watch` runs unattended and fires on every save. A person editing in the Looker IDE
while a watch runs is an ordinary event, and an overwrite there is silent: the IDE's copy is
replaced and nothing reports it. Uncommitted work in a dev workspace has no history to recover
it from. One-shot `push` already refuses in this case, and a tool that writes more often and
with less attention should not be the less careful one. A held file costs a moment of the
user's attention; an overwritten one costs work that cannot be recovered.

**Alternatives considered.**

- **Overwrite, as Google's Looker VS Code extension (1.0.0) does by default.** Simplest, and no
  message to act on. Rejected for the reason above.
- **Compare-and-set in Looker.** The right fix if it existed. The write response is `{}` with no
  `ETag` or `Last-Modified`, so there is nothing to compare against
  ([looker-http-behaviour](../reference/looker-http-behaviour.md)).
- **Merge the two versions.** `.looker-sync/state.json` keeps only hashes, so there is no common
  ancestor to merge from, and a wrong LookML merge is worse than a held file.
- **Take Looker's copy over the local file.** This destroys the save the user just made, and a
  tool started to push local work should not rewrite it.
- **Stop the watch.** One conflict would halt syncing of unrelated files, and exit codes reach
  nobody in a long-running terminal.

**Cost.** Every write needs a read first, about 345 ms. The read starts when the change is first
detected, in parallel with the settle wait, so it costs little of the save-to-`ack` time. The
guard is not airtight: an edit in Looker between that read and the write is still overwritten,
because the API offers no way to close that gap. And a held file stays out of Looker until a
person acts on it.
